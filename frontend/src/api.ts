const apiBaseUrl = '/api/v1';

type ApiError = {
  detail?: string | { msg?: string }[] | Record<string, unknown>;
};

function csrfToken(): string | undefined {
  const cookie = document.cookie
    .split('; ')
    .find((value) => value.startsWith('csrf_token='));
  return cookie ? decodeURIComponent(cookie.slice('csrf_token='.length)) : undefined;
}

export async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase();
  const headers = new Headers(options.headers);
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = csrfToken();
    if (token) headers.set('X-CSRF-Token', token);
  }

  const response = await fetch(`${apiBaseUrl}${path}`, {
    ...options,
    method,
    headers,
    credentials: 'include',
  });
  if (response.status === 204) return undefined as T;
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiError;
    const message = typeof body.detail === 'string'
      ? body.detail
      : Array.isArray(body.detail)
        ? body.detail.map((issue) => issue.msg ?? 'Invalid request').join(', ')
        : body.detail
          ? JSON.stringify(body.detail)
          : `Request failed (${response.status})`;
    throw new Error(message);
  }
  return (await response.json()) as T;
}

export async function downloadCsv(path: string, filename: string): Promise<void> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    credentials: 'include',
  });
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