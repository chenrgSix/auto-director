import { AlertCircle, Film, Loader2 } from 'lucide-react';
import type { ReactNode } from 'react';
import { assetUrl } from './api';
import { STATUS } from './types';

export function Badge({ status }: { status: string }) { return <span className={`badge ${status.toLowerCase()}`}><i />{STATUS[status] ?? status}</span>; }
export function Empty({ title, children }: { title: string; children?: ReactNode }) { return <div className="empty"><Film size={30} strokeWidth={1.2} /><h3>{title}</h3><p>{children}</p></div>; }
export function Loading() { return <div className="empty"><Loader2 className="spin" />加载中…</div>; }
export function ErrorNotice({ children }: { children: ReactNode }) { return <div className="notice error" role="alert"><AlertCircle size={17} /><div>{children}</div></div>; }
export function Media({ id, kind, label }: { id: string | null; kind: 'image' | 'video'; label: string }) {
  return <figure className="media"><div className="media-content">{id ? kind === 'video' ? <video key={id} src={assetUrl(id)} controls preload="metadata" aria-label={label} /> : <img src={assetUrl(id)} alt={label} loading="lazy" /> : <div className="placeholder"><Film size={24} /><span>等待生成</span></div>}</div><figcaption>{label}</figcaption></figure>;
}
