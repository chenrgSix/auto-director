import type { ScriptReview } from './types';

const labels: Record<string, string> = { title: '标题', purpose: '叙事目的', action: '画面动作', camera: '镜头语言', start_state: '起始状态', end_state: '结束状态', transition_from_previous: '转场' };

export function ScriptReviewNotice({ review, selectShot, busy }: { review?: ScriptReview | null; selectShot: (index: number) => void; busy: boolean }) {
  if (!review || review.status === 'pending') return null;
  const fixes = review.fixes ?? [], issues = review.issues ?? [];
  return <section className="panel script-review" aria-label="AI 剧本审查">
    <div className="panel-title">AI 剧本审查<span>{review.status === 'needs_attention' ? '有问题需要你检查' : fixes.length ? `已修正 ${fixes.length} 个镜头` : '未发现明确问题'}</span></div>
    <p>{review.summary}</p>
    <small className="muted">已检查剧情因果、角色与场景衔接、动作时长、首尾状态及转场。审查针对导演剧本，不代表后续提示词或实际画面已通过质检。</small>
    {review.edited_after_review && <p className="muted">审查后你修改过分镜，当前修改尚未重新经过 AI 审查。</p>}
    {!!issues.length && <div role="alert"><strong>开始生成前，请检查这些问题</strong><ul>{issues.map((issue, index) => <li key={index}><strong>{issue.shot_index == null ? '全片' : `第 ${issue.shot_index + 1} 镜`}：</strong>{issue.message}<p>{issue.suggestion}</p>{issue.shot_index != null && <button className="text-button" type="button" disabled={busy} onClick={() => selectShot(issue.shot_index!)}>查看此镜</button>}</li>)}</ul></div>}
    {!!fixes.length && <details><summary>查看自动修正与原因（{fixes.length} 处）</summary>{fixes.map(fix => <div className="script-fix" key={fix.shot_index}><button className="text-button" type="button" disabled={busy} onClick={() => selectShot(fix.shot_index)}>第 {fix.shot_index + 1} 镜</button><p>{fix.reason}</p>{Object.entries(fix.after).map(([field, text]) => <div key={field}><strong>{labels[field] ?? field}</strong><p className="muted">原文：{fix.before[field]}</p><p>修正：{text}</p></div>)}</div>)}</details>}
  </section>;
}
