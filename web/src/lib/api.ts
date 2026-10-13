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
  if (response.status === 401) window.dispatchEvent(new Event('erp-session-expired'));
  let data;
  try {
    data = JSON.parse(await response.text());
  } catch {
    const message = [502, 503, 504].includes(response.status)
      ? `服务暂时不可用（HTTP ${response.status}），请稍后重试。`
      : `接口返回了非 JSON 内容（HTTP ${response.status}），请检查后端服务与 /api 转发。`;
    throw new ApiError(message, 'invalid_api_response', response.status);
  }
  if (!response.ok) {
    throw new ApiError(
      data?.error?.message || `请求失败 ${response.status}`,
      data?.error?.code || '',
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
