const apiBaseUrl = '/api/v1';

type ApiError = {
  detail?: string | { msg?: string; code?: string; message?: string }[] | {
    msg?: string;
    code?: string;
    message?: string;
  };
};

export class ApiRequestError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message);
    this.name = 'ApiRequestError';
  }
}

function csrfToken(): string | undefined {
  const cookie = document.cookie
    .split('; ')
    .find((value) => value.startsWith('csrf_token='));
  return cookie ? decodeURIComponent(cookie.slice('csrf_token='.length)) : undefined;
}

const inFlightRequests = new Map<string, Promise<unknown>>();

function safeRequestUrlForLog(rawUrl: string): string {
  const url = new URL(rawUrl, window.location.origin);
  for (const key of [...url.searchParams.keys()]) {
    if (/token|password|pin|otp|secret|authorization|client_id|code/i.test(key)) {
      url.searchParams.set(key, '[redacted]');
    }
  }
  return `${url.pathname}${url.search}`;
}

export function isAbortError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'name' in error
    && error.name === 'AbortError';
}

async function performApiRequest<T>(path: string, options: RequestInit): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase();
  const headers = new Headers(options.headers);
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = csrfToken();
    if (token) headers.set('X-CSRF-Token', token);
  }

  const requestUrl = `${apiBaseUrl}${path}`;
  let response: Response;
  try {
    response = await fetch(requestUrl, {
      ...options,
      method,
      headers,
      credentials: 'include',
    });
  } catch (error) {
    console.error('[api] network request failed', { method, url: safeRequestUrlForLog(requestUrl), error });
    throw error;
  }
  if (response.status === 204) return undefined as T;
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiError;
    const detail = body.detail;
    const code = detail && !Array.isArray(detail) && typeof detail === 'object'
      ? detail.code
      : undefined;
    const message = typeof body.detail === 'string'
      ? body.detail
      : Array.isArray(body.detail)
        ? body.detail.map((issue) => issue.msg ?? 'Invalid request').join(', ')
        : body.detail && typeof body.detail === 'object'
          ? body.detail.message ?? 'The request could not be completed.'
          : `Request failed (${response.status})`;
    throw new ApiRequestError(message, response.status, code);
  }
  return (await response.json()) as T;
}

export function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const url = `${apiBaseUrl}${path}`;
  const method = (options.method ?? 'GET').toUpperCase();
  const body = typeof options.body === 'string' ? options.body : '';
  const key = `${method}:${url}:${body}`;
  if (options.signal || (options.body !== undefined && typeof options.body !== 'string')) {
    return performApiRequest<T>(path, options);
  }
  const existing = inFlightRequests.get(key);
  if (existing) return existing as Promise<T>;

  const request = performApiRequest<T>(path, options);
  inFlightRequests.set(key, request);
  void request.finally(() => {
    if (inFlightRequests.get(key) === request) inFlightRequests.delete(key);
  }).catch(() => undefined);
  return request;
}

export async function downloadCsv(path: string, filename: string): Promise<void> {
  const requestUrl = `${apiBaseUrl}${path}`;
  let response: Response;
  try {
    response = await fetch(requestUrl, { credentials: 'include' });
  } catch (error) {
    console.error('[api] network request failed', { method: 'GET', url: safeRequestUrlForLog(requestUrl), error });
    throw error;
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiError;
    const message = typeof body.detail === 'string'
      ? body.detail
      : Array.isArray(body.detail)
        ? body.detail.map((issue) => issue.msg ?? 'Request failed').join(', ')
        : body.detail
          ? JSON.stringify(body.detail)
          : `Request failed (${response.status})`;
    throw new Error(message);
  }

  const objectUrl = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}
