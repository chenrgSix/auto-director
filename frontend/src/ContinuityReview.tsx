import { useRef, useState } from 'react';
import { api, useResource } from './api';
import type { Episode, Shot } from './types';
import { ErrorNotice } from './ui';

type ReviewContext = {
  version: number; review_key: string; limitations: string;
  review: { status: string; notes?: string };
  frames: { label: string; url: string }[];
};

export function ContinuityReview({ episode, shot, refresh }: { episode: Episode; shot: Shot; refresh: () => void }) {
  const [open, setOpen] = useState(false);
  return <details className="panel details-panel" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>镜头衔接复核 · {({ passed: '已复核', needs_changes: '需要修改', stale: '素材已变化，需重看' } as Record<string, string>)[shot.continuity_review_status?.status ?? ''] ?? '待复核'}</summary>
    {open && <ReviewForm key={`${shot.id}:${shot.video_asset_id}:${shot.start_frame_asset_id}`} episode={episode} shot={shot} refresh={refresh} />}
  </details>;
}

function ReviewForm({ episode, shot, refresh }: { episode: Episode; shot: Shot; refresh: () => void }) {
  const path = `/episodes/${episode.id}/shots/${shot.id}/continuity-review`;
  const resource = useResource<ReviewContext>(path);
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const receipt = useRef<{ request_id: string; expected_version: number; review_key: string; verdict: string; notes: string } | null>(null);
  const context = resource.data;
  async function save(verdict: 'passed' | 'needs_changes') {
    if (!context) return;
    const request = { expected_version: context.version, review_key: context.review_key, verdict, notes: notes.trim() };
    const previous = receipt.current;
    if (!previous || previous.expected_version !== request.expected_version || previous.review_key !== request.review_key || previous.verdict !== verdict || previous.notes !== request.notes) receipt.current = { request_id: crypto.randomUUID(), ...request };
    setBusy(true); setError(undefined);
    try { await api(path, 'POST', receipt.current); receipt.current = null; resource.refresh(); refresh(); }
    catch (cause) { setError((cause as Error).message); resource.refresh(); }
    finally { setBusy(false); }
  }
  return <div className="continuity-review">
    <p>对照前镜结尾与本镜开头，检查人物站位、视线方向、道具归属和动作是否接上。切换景别可以改变构图，人物与道具状态仍应合理。</p>
    {(resource.error || error) && <ErrorNotice>{error ?? resource.error}</ErrorNotice>}
    {context && <><div className="continuity-frames">{context.frames.map(frame => <figure key={frame.url}><img src={frame.url} alt={frame.label} loading="lazy" /><figcaption>{frame.label}</figcaption></figure>)}</div>
      <small className="muted">{context.limitations}</small>
      {context.review.notes && <p><strong>已记录的观察：</strong>{context.review.notes}</p>}
      <label className="field">画面观察与修改建议<textarea rows={3} value={notes} maxLength={3000} onChange={event => setNotes(event.target.value)} placeholder="例如：前镜公文已展开，本镜仍保持展开；人物站位和视线一致。" /></label>
      <div className="actions"><button disabled={busy || !notes.trim() || !context.frames.length} onClick={() => void save('needs_changes')}>记录需要修改</button><button disabled={busy || !notes.trim() || !context.frames.length} onClick={() => void save('passed')}>确认已复核</button><button disabled={busy} onClick={() => resource.refresh()}>重新读取画面</button></div>
      <small className="muted">记录结论不会重跑镜头。需要重新生成时，使用下方的镜头重跑入口。</small>
    </>}
  </div>;
}
