export class ApiError extends Error {
  constructor(
    message: string,
    public code: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, credentials: 'same-origin' });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('erp-session-expired'));
    throw new ApiError(
      data.error?.message || `请求失败 ${response.status}`,
      data.error?.code || '',
      response.status,
    );
  }
  return data as T;
}
export const post = <T>(path: string, data: unknown) =>
  request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
