// src/api/client.ts
//
// Refresh token flow:
//
//   1. Every request gets Authorization: Bearer <accessToken> attached.
//
//   2. On a 401 response the interceptor:
//      a. Queues any subsequent requests that arrive while the refresh is
//         in-flight (so we don't fire multiple /auth/refresh calls).
//      b. Calls POST /auth/refresh with the stored refresh token.
//      c. On success → stores the new token pair, flushes the queue with
//         the new access token, retries the original request.
//      d. On failure (refresh token expired / revoked) → calls logout(),
//         which zeroes the store and lets AuthGuard redirect to login.
//
//   Requests to /auth/login, /auth/register, and /auth/refresh are
//   excluded from the retry logic to avoid infinite loops.

import axios, {
  AxiosError,
  InternalAxiosRequestConfig,
  create as axiosCreate,
} from 'axios';
import { useAuthStore } from '../store/auth-store';

const BASE_URL = process.env.EXPO_PUBLIC_API_BASE_URL;

if (!BASE_URL) {
  throw new Error(
    'EXPO_PUBLIC_API_BASE_URL is not set — check your .env file (see mobile/.env.example). ' +
      'Switching between local/staging/production backends is done by changing this env var, ' +
      'not by editing this file.'
  );
}

const apiClient = axiosCreate({
  baseURL: BASE_URL,
  headers: {
    'Content-Type': 'application/json',
    Accept: 'application/json',
    'ngrok-skip-browser-warning': 'true',
  },
});

// Routes that must never be retried with a refreshed token
const AUTH_ROUTES = ['/auth/login', '/auth/register', '/auth/refresh', '/auth/logout'];

const isAuthRoute = (url?: string) =>
  AUTH_ROUTES.some((r) => url?.includes(r));

// Refresh-in-flight state + retry queue 

let isRefreshing = false;

type QueueItem = {
  resolve: (token: string) => void;
  reject: (err: unknown) => void;
};

let queue: QueueItem[] = [];

const flushQueue = (token: string | null, error: unknown = null) => {
  queue.forEach((item) => {
    if (token) item.resolve(token);
    else item.reject(error);
  });
  queue = [];
};

// Request interceptor — attach Bearer token 
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = useAuthStore.getState().accessToken;

    // Don't override an Authorization header the caller already set
    // (e.g. resetPassword() sets its own Bearer <reset_token>).
    const hasExplicitAuthHeader = !!config.headers?.Authorization;

    if (token && !isAuthRoute(config.url) && !hasExplicitAuthHeader) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Response interceptor — handle 401 with refresh + retry 
apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & {
      _retry?: boolean;
    };

    const is401 = error.response?.status === 401;
    const alreadyRetried = originalRequest._retry;
    const isAuth = isAuthRoute(originalRequest.url);
    const skipRefresh = originalRequest._skipAuthRefresh;

    // Pass through: not a 401, already retried, or an auth-route itself, or explicitly opted out (e.g reset-password)
    if (!is401 || alreadyRetried || isAuth || skipRefresh) {
      // If the refresh itself returned 401, log the user out
      if (is401 && isAuthRoute(originalRequest.url)) {
        useAuthStore.getState().logout();
      }
      return Promise.reject(error);
    }

    // If a refresh is already in flight, queue this request
    if (isRefreshing) {
      return new Promise<string>((resolve, reject) => {
        queue.push({ resolve, reject });
      }).then((newToken) => {
        originalRequest.headers.Authorization = `Bearer ${newToken}`;
        return apiClient(originalRequest);
      });
    }

    // Start refreshing
    originalRequest._retry = true;
    isRefreshing = true;

    const refreshToken = useAuthStore.getState().refreshToken;

    if (!refreshToken) {
      isRefreshing = false;
      useAuthStore.getState().logout();
      flushQueue(null, error);
      return Promise.reject(error);
    }

    try {
      // Call refresh without going through the interceptor again
      const { data } = await axios.post<{
        access_token: string;
        refresh_token: string;
      }>(`${BASE_URL}/auth/refresh`, { refresh_token: refreshToken });
      const { access_token, refresh_token } = data;

      // Persist the rotated pair
      useAuthStore.getState().setTokens(access_token, refresh_token);

      // Retry the original request with the new token
      originalRequest.headers.Authorization = `Bearer ${access_token}`;

      // Flush any requests that queued while we were refreshing
      flushQueue(access_token);

      return apiClient(originalRequest);
    } catch (refreshError) {
      // Refresh token expired / revoked — force logout
      flushQueue(null, refreshError);
      useAuthStore.getState().logout();
      return Promise.reject(refreshError);
    } finally {
      isRefreshing = false;
    }
  }
);

export default apiClient;