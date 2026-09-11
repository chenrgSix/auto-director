import { useCallback, useEffect, useState } from 'react';
import type { Problem } from './types';

export class ApiError extends Error {
  problem: Problem;
  constructor(problem: Problem) { super(problem.message); this.problem = problem; }
}

export async function api<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const form = body instanceof FormData;
  const response = await fetch(`/api/v1${path}`, {
    method, signal, headers: body && !form ? { 'Content-Type': 'application/json' } : {},
    body: body ? (form ? body : JSON.stringify(body)) : undefined,
  });
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => ({ error: { code: 'SERVER_UNAVAILABLE', message: '后端未返回有效数据，请检查服务是否启动。' } }));
  if (!response.ok || data.error?.code === 'SERVER_UNAVAILABLE') throw new ApiError(data.error ?? { code: 'REQUEST_FAILED', message: `请求失败 (${response.status})` });
  return data as T;
}

export function useResource<T>(path: string, interval = 0) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  useEffect(() => {
    let disposed = false;
    let pending = false;
    let controller: AbortController | undefined;
    const load = async () => {
      if (pending) return;
      pending = true;
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 15000);
      try {
        const result = await api<T>(path, 'GET', undefined, controller.signal);
        if (!disposed) { setData(result); setError(undefined); }
      } catch (error) { if (!disposed) setError(error instanceof Error ? error.message : '请求失败'); }
      finally { window.clearTimeout(timeout); pending = false; }
    };
    void load();
    const timer = interval ? window.setInterval(() => void load(), interval) : undefined;
    const online = () => void load();
    window.addEventListener('online', online);
    return () => { disposed = true; controller?.abort(); window.clearInterval(timer); window.removeEventListener('online', online); };
  }, [path, interval, revision]);
  return { data, error, refresh };
}

export const assetUrl = (id: string, download = false) => `/api/v1/assets/${encodeURIComponent(id)}/file${download ? '?download=true' : ''}`;
