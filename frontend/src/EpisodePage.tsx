import { useState } from 'react';
import { ArrowDown, ArrowLeft, ArrowUp, Download, GripVertical, Play, Plus, RefreshCw, Square, Trash2 } from 'lucide-react';
import { api, assetUrl, useResource } from './api';
import { navigate, type Notify } from './App';
import { ACTIVE, type Episode, type Job, type Shot } from './types';
import { Badge, Empty, ErrorNotice, Loading, Media } from './ui';
import { EpisodeWorkflows } from './EpisodeWorkflows';

export function EpisodesPage({ notify }: { notify: Notify }) {
  const resource = useResource<Episode[]>('/episodes', 4000);
  async function remove(episode: Episode) {
    if (!window.confirm(`删除「${episode.title || episode.idea}」及其全部生成资产？此操作无法撤销。`)) return;
    try { await api(`/episodes/${episode.id}?delete_assets=true`, 'DELETE'); resource.refresh(); notify('短片与生成资产已删除'); }
    catch (error) { notify((error as Error).message, true); }
  }
  return <><div className="section-heading"><div><span className="eyebrow">YOUR FILM LIBRARY</span><h1>我的短片</h1><p>每一个想法，都有自己的创作轨迹。</p></div><a className="primary" href="#create"><Plus size={16} />新建短片</a></div>{resource.error && <ErrorNotice>{resource.error}</ErrorNotice>}{!resource.data ? <Loading /> : !resource.data.length ? <Empty title="第一部短片，等待你的想法">点击「新建短片」，开始你的创作。</Empty> : <div className="episode-grid">{resource.data.map(episode => <article className="episode-card" key={episode.id}><a href={`#episode/${episode.id}`} className="episode-cover">{episode.final_video_asset_id ? <video src={assetUrl(episode.final_video_asset_id)} preload="metadata" muted /> : episode.shots[0]?.start_frame_asset_id ? <img src={assetUrl(episode.shots[0].start_frame_asset_id)} alt={episode.title || '短片首帧'} /> : <div className="empty-cover"><Play size={32} strokeWidth={1} /><span>IDEA / {episode.target_duration}s</span></div>}<span className="cover-duration">{episode.target_duration}s · {episode.aspect_ratio}</span></a><div className="episode-card-body"><Badge status={episode.status} /><a href={`#episode/${episode.id}`}><h3>{episode.title || episode.idea}</h3></a><p>{episode.style} · {new Date(episode.created_at).toLocaleDateString('zh-CN')}</p><button className="icon-button delete" aria-label={`删除 ${episode.title || episode.idea}`} disabled={ACTIVE.has(episode.status)} onClick={() => void remove(episode)}><Trash2 size={15} /></button></div></article>)}</div>}</>;
}

export function EpisodePage({ id, notify }: { id: string; notify: Notify }) {
  const resource = useResource<Episode>(`/episodes/${id}`, 1500);
  const jobs = useResource<Job[]>(`/jobs?episode_id=${id}`, 2000);
  const [selected, setSelected] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [dragged, setDragged] = useState<string>();
  const episode = resource.data;
  if (resource.error) return <ErrorNotice>{resource.error}</ErrorNotice>;
  if (!episode) return <Loading />;
  const active = ACTIVE.has(episode.status);
  const shot = episode.shots.find(item => item.id === selected) || episode.shots[0];
  const total = episode.shots.filter(item => item.enabled).length;
  const passed = episode.shots.filter(item => item.enabled && item.status === 'PASSED').length;
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
  return <>
    <button className="text-button back" onClick={() => navigate('episodes')}><ArrowLeft size={16} />我的短片</button>
    <div className="section-heading"><div><div className="eyebrow">EPISODE / {id.slice(0, 8)}</div><h1 className="episode-title">{episode.title || episode.idea}</h1><p>{episode.target_duration} 秒 · {episode.aspect_ratio} · {episode.style}</p></div><div className="actions"><Badge status={episode.status} />{active ? <button disabled={busy} onClick={() => void action(`/episodes/${id}/cancel`, '已请求取消，正在停止当前任务')}><Square size={14} />取消任务</button> : <><button disabled={busy || !total || passed !== total} onClick={() => void action(`/episodes/${id}/compose`, '已安排重新导出')}><RefreshCw size={15} />重新导出</button>{episode.status !== 'COMPLETED' && <button className="primary" disabled={busy} onClick={() => void action(`/episodes/${id}/generate`, '已继续生成')}><Play size={15} />继续生成</button>}</>}</div></div>
    {episode.error && <ErrorNotice><strong>{episode.error.message}</strong><small>{episode.error.code}</small>{episode.error.details != null && <details><summary>查看错误详情</summary><pre>{JSON.stringify(episode.error.details, null, 2)}</pre></details>}</ErrorNotice>}
    <EpisodeWorkflows episode={episode} busy={busy} setBusy={setBusy} locked={active || !jobs.data || jobs.data.some(job => ['QUEUED', 'RUNNING', 'UNKNOWN'].includes(job.status))} onSaved={() => { resource.refresh(); jobs.refresh(); }} notify={notify} />
    {episode.warnings.map(message => <div className="notice" key={message}>{message}</div>)}
    {active && <div className="progress-panel"><div><span>正在推进创作</span><strong>{passed} / {total || '—'} 镜头</strong></div><div className="progress-track"><i style={{ width: `${total ? passed / total * 90 : 3}%` }} /></div><small>进度会自动更新，可以离开页面，稍后回来查看。</small></div>}
    {episode.final_video_asset_id && <section className="final-panel"><div><span className="eyebrow">IT'S A WRAP</span><h2>短片已完成。</h2><p>{episode.final_duration?.toFixed(2)} 秒 · MP4</p><a className="primary" href={assetUrl(episode.final_video_asset_id, true)} download><Download size={16} />下载成片</a></div><video src={assetUrl(episode.final_video_asset_id)} controls preload="metadata" aria-label="最终短片" /></section>}
    {!episode.shots.length ? <Empty title={active ? '导演正在规划故事' : '你的故事，即将成为镜头'}>计划完成后，镜头、首尾帧和视频将在这里出现。</Empty> : <div className="editing-grid"><aside className="panel shot-list"><div className="panel-title">镜头时间线 <span>{episode.shots.length} SHOTS</span></div>{episode.shots.map((item, index) => <div key={item.id} className={`shot-row ${shot?.id === item.id ? 'selected' : ''} ${!item.enabled ? 'disabled' : ''}`} draggable={!active && !busy} onDragStart={() => setDragged(item.id)} onDragOver={event => event.preventDefault()} onDrop={() => { const from = episode.shots.findIndex(item => item.id === dragged); if (from >= 0) move(from, index); setDragged(undefined); }}><button className="shot-select" onClick={() => setSelected(item.id)}><span className="shot-number">{String(index + 1).padStart(2, '0')}</span><div><strong>{item.title}</strong><small>{item.duration.toFixed(2)}s · {item.transition_from_previous}</small><Badge status={item.status} /></div><GripVertical size={14} /></button><div className="shot-controls"><label><input type="checkbox" checked={item.enabled} disabled={active || busy} onChange={event => void updateTimeline(episode.shots.map(s => s.id === item.id ? { ...s, enabled: event.target.checked } : s))} />使用</label><button className="icon-button" aria-label={`上移镜头 ${index + 1}`} disabled={active || busy || index === 0} onClick={() => move(index, index - 1)}><ArrowUp size={14} /></button><button className="icon-button" aria-label={`下移镜头 ${index + 1}`} disabled={active || busy || index === episode.shots.length - 1} onClick={() => move(index, index + 1)}><ArrowDown size={14} /></button></div></div>)}</aside>
      {shot && <section className="shot-detail"><div className="panel"><div className="panel-title">{shot.title}<Badge status={shot.status} /></div><p className="shot-description">{shot.action}</p><small className="muted">{shot.camera}</small><div className="keyframe-grid"><Media id={shot.start_frame_asset_id} kind="image" label="首帧 / START" /><Media id={shot.end_frame_asset_id} kind="image" label="尾帧 / END" /></div><Media id={shot.video_asset_id} kind="video" label="镜头预览 / CLIP" /><div className="actions retry-actions"><button disabled={busy || active} onClick={() => void action(`/shots/${shot.id}/retry-keyframes`, '已安排重试关键帧与视频')}><RefreshCw size={14} />重试首尾帧</button><button disabled={busy || active || !shot.start_frame_asset_id || !shot.end_frame_asset_id} onClick={() => void action(`/shots/${shot.id}/retry-video`, '已安排单独重试视频')}><RefreshCw size={14} />仅重试视频</button></div>{shot.error && <ErrorNotice>{shot.error.message}</ErrorNotice>}</div>
      {shot.prompts && <details className="panel details-panel"><summary>镜头提示词与质量记录</summary><label>视频提示词</label><pre>{shot.prompts.video_prompt}</pre><label>负面约束</label><pre>{shot.prompts.negative_prompt}</pre>{shot.qa.map((qa, index) => <div className="qa-result" key={index}><strong>{qa.stage} · {qa.retry_scope ? '需要重试' : '通过阈值'}</strong><span>角色 {Math.round(qa.character_consistency * 100)} · 场景 {Math.round(qa.scene_consistency * 100)} · 动作 {Math.round(qa.action_accuracy * 100)}</span><p>{qa.explanation}</p></div>)}</details>}</section>}
    </div>}
    {Object.keys(episode.references).length > 0 && <details className="panel details-panel"><summary>单集参考资产 · {Object.keys(episode.references).length}</summary><div className="reference-grid">{Object.entries(episode.references).map(([name, asset]) => <Media key={name} id={asset} kind="image" label={name} />)}</div></details>}
    <details className="panel details-panel"><summary>作业记录与生成统计</summary><div className="metrics">{Object.entries(episode.metrics).map(([key, value]) => <div key={key}><strong>{value}</strong><span>{key}</span></div>)}</div><JobList jobs={jobs.data || []} notify={notify} onChange={jobs.refresh} /><details><summary>计划与视觉设定</summary><pre>{JSON.stringify({ plan: episode.plan, bible: episode.bible }, null, 2)}</pre></details>{!!episode.workflow_binding_history?.length && <details><summary>更换前的计划与生成记录 · {episode.workflow_binding_history.length} 次</summary><pre>{JSON.stringify(episode.workflow_binding_history, null, 2)}</pre></details>}</details>
  </>;
}

export function JobList({ jobs, notify, onChange }: { jobs: Job[]; notify: Notify; onChange: () => void }) {
  async function reconcile(job: Job, resolve = false) {
    const note = resolve ? window.prompt('仅在已核对 ComfyUI 队列和历史、确认此任务可重新提交后填写结论（至少 10 字）。此操作将解除防重复提交保护。') : null;
    if (resolve && !note) return;
    try { await api(`/jobs/${job.id}/${resolve ? 'resolve' : 'reconcile'}`, 'POST', resolve ? { note } : undefined); onChange(); notify(resolve ? '人工核对结论已记录' : '已取得 prompt_id；可继续单集或恢复试跑'); }
    catch (error) { notify((error as Error).message, true); }
  }
  if (!jobs.length) return <p className="muted">尚无渲染作业。</p>;
  return <div className="job-list">{jobs.map(job => <div className="job" key={job.id}><div><strong>{job.type}</strong><Badge status={job.status} /></div><small>{job.id.slice(0, 8)} · prompt: {job.comfy_prompt_id || '尚未取得'}</small>{job.progress?.max && <progress value={job.progress.value} max={job.progress.max} />}{job.error && <p className="error-text">{job.error.message}</p>}{job.status === 'UNKNOWN' && <div className="actions"><button onClick={() => void reconcile(job)}>核对 ComfyUI 历史</button><button onClick={() => void reconcile(job, true)}>人工确认可重新提交</button>{job.type === 'WORKFLOW_TEST' && job.comfy_prompt_id && <button onClick={() => { void api(`/jobs/${job.id}/resume`, 'POST').then(() => { notify('已恢复试跑'); onChange(); }).catch(error => notify(error.message, true)); }}>恢复试跑</button>}</div>}</div>)}</div>;
}
