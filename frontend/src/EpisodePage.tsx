import { useRef, useState } from 'react';
import { ArrowDown, ArrowLeft, ArrowUp, Download, GripVertical, Play, Plus, RefreshCw, Square, Trash2 } from 'lucide-react';
import { api, assetUrl, useResource } from './api';
import { navigate, type Notify } from './App';
import { ACTIVE, STATUS, type Episode, type Job, type Shot } from './types';
import { Badge, Empty, ErrorNotice, Loading, Media } from './ui';
import { EpisodeWorkflows } from './EpisodeWorkflows';
import { EpisodePreview } from './EpisodePreview';
import { EpisodeRerun, RerunHistory } from './EpisodeRerun';
import { EpisodeQuality } from './EpisodeQuality';
import { EpisodeQAPolicy } from './EpisodeQAPolicy';
import { PromptOptimization } from './PromptOptimization';
import { PromptPreparation } from './PromptPreparation';

export function EpisodesPage({ notify }: { notify: Notify }) {
  const resource = useResource<Episode[]>('/episodes', 4000);
  async function remove(episode: Episode) {
    if (!window.confirm(`删除「${episode.title || episode.idea}」及其全部生成资产？此操作无法撤销。`)) return;
    try { await api(`/episodes/${episode.id}?delete_assets=true`, 'DELETE'); resource.refresh(); notify('短片与生成资产已删除'); }
    catch (error) { notify((error as Error).message, true); }
  }
  return <><div className="section-heading"><div><span className="eyebrow">YOUR FILM LIBRARY</span><h1>我的短片</h1><p>每一个想法，都有自己的创作轨迹。</p></div><a className="primary" href="#create"><Plus size={16} />新建短片</a></div>{resource.error && <ErrorNotice>{resource.error}</ErrorNotice>}{!resource.data ? <Loading /> : !resource.data.length ? <Empty title="第一部短片，等待你的想法">点击「新建短片」，开始你的创作。</Empty> : <div className="episode-grid">{resource.data.map(episode => <article className="episode-card" key={episode.id}><a href={`#episode/${episode.id}`} className="episode-cover">{episode.final_video_asset_id ? <video src={assetUrl(episode.final_video_asset_id)} preload="metadata" muted /> : episode.shots[0]?.start_frame_asset_id ? <img src={assetUrl(episode.shots[0].start_frame_asset_id)} alt={episode.title || '短片首帧'} /> : <div className="empty-cover"><Play size={32} strokeWidth={1} /><span>IDEA / {episode.target_duration}s</span></div>}<span className="cover-duration">{episode.final_duration != null ? `${episode.final_duration.toFixed(2)}s` : `参考 ${episode.target_duration}s`} · {episode.aspect_ratio}</span></a><div className="episode-card-body"><Badge status={episode.status} /><a href={`#episode/${episode.id}`}><h3>{episode.title || episode.idea}</h3></a><p>{episode.style} · {new Date(episode.created_at).toLocaleDateString('zh-CN')}</p><button className="icon-button delete" aria-label={`删除 ${episode.title || episode.idea}`} disabled={ACTIVE.has(episode.status)} onClick={() => void remove(episode)}><Trash2 size={15} /></button></div></article>)}</div>}</>;
}

export function EpisodePage({ id, notify }: { id: string; notify: Notify }) {
  const resource = useResource<Episode>(`/episodes/${id}`, 1500);
  const jobs = useResource<Job[]>(`/jobs?episode_id=${id}`, 2000);
  const [selected, setSelected] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [dragged, setDragged] = useState<string>();
  const diagnostics = useRef<HTMLDetailsElement>(null);
  const episode = resource.data;
  if (resource.error && !episode) return <ErrorNotice>{resource.error}</ErrorNotice>;
  if (!episode || episode.id !== id) return <Loading />;
  const active = ACTIVE.has(episode.status);
  const disconnected = !!(resource.error || jobs.error);
  const uncertain = jobs.data?.filter(job => job.status === 'UNKNOWN') || [];
  const reconnecting = jobs.data?.find(job => job.status === 'RUNNING' && job.progress?.connection === 'reconnecting');
  const recovering = uncertain.length > 0;
  const reviewing = episode.preview_required && !episode.preview_approved_at;
  const shot = episode.shots.find(item => item.id === selected) || episode.shots[0];
  const total = episode.shots.filter(item => item.enabled).length;
  const completed = episode.shots.filter(item => item.enabled && item.status === 'PASSED').length;
  const reviewShots = episode.shots.filter(item => item.enabled && item.status === 'PASSED' && item.needs_review);
  const pendingReviews = episode.shots.filter(item => item.enabled && ['pending', 'running'].includes(item.visual_review?.status ?? ''));
  const currentShot = episode.shots.find(item => item.enabled && ['GENERATING_START_FRAME', 'START_FRAME_READY', 'GENERATING_END_FRAME', 'KEYFRAMES_READY', 'RENDERING_VIDEO', 'VIDEO_READY', 'QA'].includes(item.status));
  const settingsLocked = active || disconnected || !jobs.data || jobs.data.some(job => ['QUEUED', 'RUNNING', 'UNKNOWN'].includes(job.status));
  function showDiagnostics() {
    if (!diagnostics.current) return;
    diagnostics.current.open = true;
    diagnostics.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function selectReviewShot(shotId: string) {
    setSelected(shotId);
    requestAnimationFrame(() => document.getElementById('episode-shot-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  }
  async function action(path: string, label: string, body?: unknown, method = 'POST') {
    setBusy(true);
    try { await api(path, method, body); resource.refresh(); jobs.refresh(); notify(label); }
    catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  async function updateTimeline(shots: Shot[]) {
    await action(`/episodes/${id}/timeline`, '时间线已更新，成片需要重新导出', { shots: shots.map(item => ({ id: item.id, enabled: item.enabled })) }, 'PATCH');
  }
  function move(from: number, to: number) {
    if (!episode || to < 0 || to >= episode.shots.length) return;
    const reordered = [...episode.shots];
    const [item] = reordered.splice(from, 1); reordered.splice(to, 0, item);
    void updateTimeline(reordered);
  }
  const progressTotal = reviewing ? episode.shots.length : total;
  const progress = active && <div className="progress-panel episode-progress"><div><span>{reviewing ? '正在准备分镜预览' : '正在推进创作'}</span><strong>{reviewing ? episode.shots.filter(item => item.prompts).length : completed} / {progressTotal || '—'} 镜头{!reviewing && '已完成'}</strong></div>{!reviewing && currentShot && <p>第 {currentShot.index + 1} 镜 · {currentShot.title}：{episode.status === 'QA' && currentShot.video_asset_id ? '视频已生成，正在检查画面；可在镜头预览查看。' : `${STATUS[currentShot.status] ?? currentShot.status}。`}{episode.status === 'QA' && (episode.qa_policy ?? 'strict') === 'advisory' && '画面问题会标记待复核并继续。'}</p>}{!reviewing && reviewShots.length > 0 && <small>{reviewShots.length} 镜已完成，画面待复核，不阻塞后续生成。</small>}<div className="progress-track"><i style={{ width: `${progressTotal ? (reviewing ? episode.shots.filter(item => item.prompts).length : completed) / progressTotal * (reviewing ? 100 : 90) : 3}%` }} /></div><div className="episode-progress-actions"><small>{reviewing ? '提示词每批保存后自动更新，可提前查看。' : '进度自动更新，已有视频可随时预览。'}</small><button className="text-button" onClick={showDiagnostics}>查看生成记录</button></div></div>;
  return <>
    <button className="text-button back episode-back" onClick={() => navigate('episodes')}><ArrowLeft size={16} />我的短片</button>
    <div className="section-heading episode-heading"><div><div className="eyebrow">EPISODE / {id.slice(0, 8)}</div><h1 className="episode-title">{episode.title || episode.idea}</h1><p>参考 {episode.target_duration} 秒 · {episode.aspect_ratio} · {episode.style}</p></div><div className="actions"><Badge status={recovering && !active ? 'UNKNOWN' : episode.status} />{active ? <button disabled={busy} onClick={() => void action(`/episodes/${id}/cancel`, '已请求取消，正在停止当前任务')}><Square size={14} />取消任务</button> : reviewing ? <>{!episode.creation_source && episode.status !== 'AWAITING_REVIEW' && <button className="primary" disabled={busy} onClick={() => void action(`/episodes/${id}/preview`, '正在准备分镜预览')}><Play size={15} />{episode.shots.length ? '继续准备分镜' : '生成分镜预览'}</button>}</> : <><button disabled={busy || !total || completed !== total} onClick={() => void action(`/episodes/${id}/compose`, '已安排重新导出')}><RefreshCw size={15} />重新导出</button>{episode.status !== 'COMPLETED' && <button className="primary" disabled={busy || disconnected || uncertain.some(job => !job.comfy_prompt_id)} onClick={() => void action(`/episodes/${id}/generate`, recovering ? '正在连接原任务并恢复生成' : '已继续生成')}><Play size={15} />{recovering ? '重新连接并恢复' : '继续生成'}</button>}</>}</div></div>
    {disconnected && <div className="notice" role="status">页面连接中断，正在自动重连。下方保留最后一次进度，后台任务可能仍在运行。<button onClick={() => { resource.refresh(); jobs.refresh(); }}>立即重连</button></div>}
    {reconnecting && <div className="notice" role="status">ComfyUI 连接中断，正在自动重连原任务（第 {reconnecting.progress?.reconnect_attempt} 次）。已有进度已保存，不会重复提交。</div>}
    {recovering && !active && <div className="notice" role="status">原任务状态尚未确认，远端可能仍在生成。{uncertain.every(job => job.comfy_prompt_id) ? '点击「重新连接并恢复」读取原任务进度或取回结果。' : '请在作业记录中先核对 ComfyUI 历史，取得任务 ID。'} 恢复不会重做已完成的镜头。</div>}
    {episode.error && !recovering && <div className="episode-alert"><ErrorNotice><strong className="episode-alert-message">{episode.error.message}</strong></ErrorNotice><button onClick={showDiagnostics}>查看错误详情</button></div>}
    {!active && (episode.warnings.length > 0 || recovering) && <div className="episode-record-link"><button className="text-button" onClick={showDiagnostics}>{recovering ? '核对未确认作业' : `${episode.warnings.length} 条生成提示 · 查看记录`}</button></div>}
    {(reviewing || !episode.shots.length) && progress}
    {episode.final_video_asset_id && <section className="final-panel episode-film" aria-label="成片预览">
      <video src={assetUrl(episode.final_video_asset_id)} controls preload="metadata" aria-label="最终短片" />
      <div className="film-summary"><span className="eyebrow">FINAL CUT</span><h2>短片已完成</h2><p>{episode.final_duration?.toFixed(2)} 秒 · MP4{episode.composition_policy === 'full_clips' && ` · 完整拼接（剧本参考 ${episode.target_duration} 秒）`}</p><a className="primary" href={assetUrl(episode.final_video_asset_id, true)} download><Download size={16} />下载成片</a>
        {reviewShots.length > 0 && <details className="film-review"><summary>{reviewShots.length} 镜待复核 · 查看镜头</summary><p>视频已保留，可查看后选择镜头重跑。</p><div className="actions">{reviewShots.map(item => <button key={item.id} onClick={() => selectReviewShot(item.id)}>查看第 {item.index + 1} 镜 · {item.title}</button>)}</div></details>}
      </div>
    </section>}
    {pendingReviews.length > 0 && <div className="notice" role="status">AI 视觉复核在后台进行：{pendingReviews.length} 镜未完成。视频生成与导出继续，复核完成后自动更新结果。</div>}
    {episode.creation_source && <div className="notice creation-provenance">来自 <a href={`#creation/${episode.creation_source.project_id}`}>创作包版本 {episode.creation_source.revision} → 返回创作</a>。此处的分镜调整仅作用于本次制作，更新故事请保存创作包新版本。{episode.creation_visual_review === 'manual' && ' 画面由你和 Codex 复核，技术检查不会调用视觉模型。'}</div>}
    {reviewing ? episode.status === 'AWAITING_REVIEW' ? <><EpisodePreview key={id} episode={episode} busy={busy} setBusy={setBusy} refresh={() => { resource.refresh(); jobs.refresh(); }} notify={notify} />{!episode.creation_source && <button className="text-button" disabled={busy} onClick={() => void action(`/episodes/${id}/preview`, '正在更新分镜预览')}>工作流配置有变化？更新分镜预览</button>}</> : episode.shots.length ? <PromptPreparation key={id} episode={episode} /> : <Empty title={active ? '导演正在准备分镜和提示词' : '继续准备分镜即可预览'}>预览准备完成后会等待你确认，再开始图片与视频生成。</Empty> : !episode.shots.length ? <Empty title={active ? '导演正在规划故事' : '你的故事，即将成为镜头'}>计划完成后，镜头、首尾帧和视频将在这里出现。</Empty> : <div className="editing-grid episode-editor">
      {shot && <section className="shot-detail" id="episode-shot-detail" aria-label="镜头预览">
        <div className="panel shot-player">
          <div className="panel-title"><strong>第 {shot.index + 1} 镜 · {shot.title}</strong><Badge status={shot.status === 'PASSED' && shot.needs_review ? 'NEEDS_REVIEW' : shot.status} /></div>
          <Media id={shot.video_asset_id || shot.start_frame_asset_id} kind={shot.video_asset_id || !shot.start_frame_asset_id ? 'video' : 'image'} label={shot.video_asset_id ? '镜头预览 / CLIP' : '关键帧预览 · 等待视频生成'} />
          <div className="shot-player-meta"><span>{shot.actual_duration != null ? `实际 ${shot.actual_duration.toFixed(2)} 秒 · ` : ''}剧本参考 {shot.duration.toFixed(2)} 秒 · {episode.aspect_ratio}</span>{shot.video_asset_id && <a href={assetUrl(shot.video_asset_id, true)} download>下载本镜</a>}</div>
          {shot.visual_review && <p className="muted">AI 视觉复核：{({ pending: '等待后台复核', running: '后台复核中，不阻塞生成', completed: '复核已完成', failed: '复核未完成，请人工查看', skipped: '未复核（已关闭、取消或素材变更）' })[shot.visual_review.status]}</p>}
          {shot.error && <ErrorNotice>{shot.error.message}</ErrorNotice>}
          {shot.needs_review && <details className="notice shot-review"><summary>画面待复核 · 查看问题</summary><span>视频已保留，可先播放，再决定是否重跑。</span>{(shot.review_notes ?? []).map((note, index) => <p key={index}><strong>{qaStageLabel(note.stage)}</strong> · {note.message}</p>)}</details>}
        </div>
        <details className="panel details-panel"><summary>分镜说明与首尾帧</summary><p className="shot-description">{shot.action}</p><small className="muted">{shot.camera}</small><div className="keyframe-grid"><Media id={shot.start_frame_asset_id} kind="image" label="首帧 / START" />{shot.end_frame_asset_id && <Media id={shot.end_frame_asset_id} kind="image" label="尾帧 / END" />}</div></details>
        {shot.prompts && <details className="panel details-panel"><summary>镜头提示词与质量记录</summary><label>视频提示词</label><pre>{shot.prompts.video_prompt}</pre><label>负面约束</label><pre>{shot.prompts.negative_prompt}</pre>{shot.qa.map((qa, index) => <div className="qa-result" key={index}><strong>{qaStageLabel(qa.stage)} · {qa.disposition === 'warning' ? '待复核 · 已保留结果' : qa.retry_scope ? '未通过 · 重试记录' : '通过阈值'}</strong><span>角色 {Math.round(qa.character_consistency * 100)} · 场景 {Math.round(qa.scene_consistency * 100)} · 动作 {Math.round(qa.action_accuracy * 100)}</span><p>{qa.explanation}</p></div>)}</details>}
        {!episode.creation_source && shot.enabled && shot.video_asset_id && shot.prompts && <PromptOptimization key={shot.id} episode={episode} shot={shot} locked={settingsLocked} busy={busy} setBusy={setBusy} refresh={() => { resource.refresh(); jobs.refresh(); }} notify={notify} />}
      </section>}
      <aside className="panel shot-list">{progress}<div className="panel-title">镜头时间线 <span>{episode.shots.length} SHOTS</span></div>{episode.shots.map((item, index) => <div key={item.id} className={`shot-row ${shot?.id === item.id ? 'selected' : ''} ${!item.enabled ? 'disabled' : ''}`} draggable={!active && !busy && !recovering && !disconnected} onDragStart={() => setDragged(item.id)} onDragOver={event => event.preventDefault()} onDrop={() => { const from = episode.shots.findIndex(item => item.id === dragged); if (from >= 0) move(from, index); setDragged(undefined); }}><button className="shot-select" onClick={() => setSelected(item.id)}><span className="shot-number">{String(index + 1).padStart(2, '0')}</span><div><strong>{item.title}</strong><small>{item.actual_duration != null ? `实际 ${item.actual_duration.toFixed(2)}s` : `参考 ${item.duration.toFixed(2)}s`} · {item.transition_from_previous}</small><Badge status={item.status === 'PASSED' && item.needs_review ? 'NEEDS_REVIEW' : item.status} /></div><GripVertical size={14} /></button><div className="shot-controls"><label><input type="checkbox" checked={item.enabled} disabled={active || busy || recovering || disconnected} onChange={event => void updateTimeline(episode.shots.map(s => s.id === item.id ? { ...s, enabled: event.target.checked } : s))} />使用</label><button className="icon-button" aria-label={`上移镜头 ${index + 1}`} disabled={active || busy || recovering || disconnected || index === 0} onClick={() => move(index, index - 1)}><ArrowUp size={14} /></button><button className="icon-button" aria-label={`下移镜头 ${index + 1}`} disabled={active || busy || recovering || disconnected || index === episode.shots.length - 1} onClick={() => move(index, index + 1)}><ArrowDown size={14} /></button></div></div>)}</aside>
    </div>}
    {!reviewing && !!episode.shots.length && <EpisodeRerun episode={episode} shot={shot} locked={active || disconnected || recovering || !jobs.data} busy={busy} setBusy={setBusy} refresh={() => { resource.refresh(); jobs.refresh(); }} notify={notify} />}
    <details className="panel details-panel episode-settings"><summary>生成设置 · 质量、质检与工作流</summary>
    <EpisodeQuality key={`${id}:quality`} episode={episode} busy={busy} setBusy={setBusy} locked={settingsLocked} onSaved={() => { resource.refresh(); jobs.refresh(); }} notify={notify} />
    <EpisodeQAPolicy key={`${id}:qa-policy`} toggleLocked={disconnected || !jobs.data} episode={episode} busy={busy} setBusy={setBusy} locked={settingsLocked} onSaved={() => { resource.refresh(); jobs.refresh(); }} notify={notify} />
    <EpisodeWorkflows episode={episode} busy={busy} setBusy={setBusy} locked={active || !jobs.data || jobs.data.some(job => ['QUEUED', 'RUNNING', 'UNKNOWN'].includes(job.status))} onSaved={() => { resource.refresh(); jobs.refresh(); }} notify={notify} />
    </details>
    <RerunHistory episode={episode} />
    {Object.keys(episode.references).length > 0 && <details className="panel details-panel"><summary>单集参考资产 · {Object.keys(episode.references).length}</summary><div className="reference-grid">{Object.entries(episode.references).map(([name, asset]) => <Media key={name} id={asset} kind="image" label={name} />)}</div></details>}
    <details className="panel details-panel episode-diagnostics" ref={diagnostics}><summary>生成记录与诊断 · {jobs.data?.length ?? 0} 个作业{episode.warnings.length > 0 && ` · ${episode.warnings.length} 条提示`}</summary>
      {episode.error && <ErrorNotice><strong>{episode.error.message}</strong><small>{episode.error.code}</small>{episode.error.details != null && <details><summary>完整错误详情</summary><pre>{JSON.stringify(episode.error.details, null, 2)}</pre></details>}</ErrorNotice>}
      {episode.warnings.map(message => <div className="notice" key={message}>{message}</div>)}
      <div className="metrics">{Object.entries(episode.metrics).map(([key, value]) => <div key={key}><strong>{value}</strong><span>{key}</span></div>)}</div><JobList jobs={jobs.data || []} notify={notify} onChange={jobs.refresh} /><details><summary>计划与视觉设定</summary><pre>{JSON.stringify({ plan: episode.plan, bible: episode.bible }, null, 2)}</pre></details>{!!episode.workflow_binding_history?.length && <details><summary>更换前的计划与生成记录 · {episode.workflow_binding_history.length} 次</summary><pre>{JSON.stringify(episode.workflow_binding_history, null, 2)}</pre></details>}</details>
  </>;
}

function qaStageLabel(stage: string) {
  return ({ keyframes: '关键帧检查', video: '视频检查', start_candidate: '首帧候选比较' } as Record<string, string>)[stage] ?? stage;
}

export function JobList({ jobs, notify, onChange }: { jobs: Job[]; notify: Notify; onChange: () => void }) {
  async function reconcile(job: Job, resolve = false) {
    const note = resolve ? window.prompt('仅在已核对 ComfyUI 队列和历史、确认此任务可重新提交后填写结论（至少 10 字）。此操作将解除防重复提交保护。') : null;
    if (resolve && !note) return;
    try { await api(`/jobs/${job.id}/${resolve ? 'resolve' : 'reconcile'}`, 'POST', resolve ? { note } : undefined); onChange(); notify(resolve ? '人工核对结论已记录' : '已取得 prompt_id；可继续单集或恢复试跑'); }
    catch (error) { notify((error as Error).message, true); }
  }
  if (!jobs.length) return <p className="muted">尚无渲染作业。</p>;
  return <div className="job-list">{jobs.map(job => <div className="job" key={job.id}><div><strong>{job.type}</strong><Badge status={job.status} /></div><small>{job.id.slice(0, 8)} · prompt: {job.comfy_prompt_id || '尚未取得'}</small>{job.progress?.max && <progress value={job.progress.value} max={job.progress.max} />}{job.status === 'RUNNING' && job.progress?.connection === 'reconnecting' && <p className="muted">连接中断，自动重连中 · 已保存节点 {job.progress.node || '等待执行'} · 第 {job.progress.reconnect_attempt} 次</p>}{job.error && <p className="error-text">{job.error.message}</p>}{job.status === 'UNKNOWN' && <div className="actions"><button onClick={() => void reconcile(job)}>核对 ComfyUI 历史</button><button onClick={() => void reconcile(job, true)}>人工确认可重新提交</button>{job.type === 'WORKFLOW_TEST' && job.comfy_prompt_id && <button onClick={() => { void api(`/jobs/${job.id}/resume`, 'POST').then(() => { notify('已恢复试跑'); onChange(); }).catch(error => notify(error.message, true)); }}>恢复试跑</button>}</div>}</div>)}</div>;
}
