import { useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api, assetUrl } from './api';
import type { Episode, Shot } from './types';
import type { Notify } from './App';
import { Media } from './ui';

type Props = { episode: Episode; shot?: Shot; locked: boolean; busy: boolean; setBusy: (value: boolean) => void; refresh: () => void; notify: Notify };

export function EpisodeRerun({ episode, shot, locked, busy, setBusy, refresh, notify }: Props) {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState('all');
  const [scope, setScope] = useState('video');
  const [newSeed, setNewSeed] = useState(true);
  const [staticFrames, setStaticFrames] = useState('keep');
  const [version, setVersion] = useState(episode.version);
  function chooseTarget(value: string) {
    setTarget(value); setScope('video'); setNewSeed(true); setStaticFrames('keep'); setVersion(episode.version); setOpen(true);
  }
  const selected = target === 'all' ? episode.shots.filter(s => s.enabled) : episode.shots.filter(s => s.id === target && s.enabled);
  let previousChanged = false;
  const affected = episode.shots.filter(s => {
    if (!s.enabled) return false;
    previousChanged = selected.some(item => item.id === s.id) || (previousChanged && ['CONTINUE_FRAME', 'CONTINUE_VIDEO'].includes(s.transition_from_previous));
    return previousChanged;
  });
  async function submit() {
    setBusy(true);
    try {
      await api(`/episodes/${episode.id}/rerun`, 'POST', { expected_version: version, scope, new_seed: newSeed, ...(staticFrames === 'keep' ? {} : { allow_static_end_frame: staticFrames === 'allow' }), ...(target === 'all' ? {} : { shot_ids: [target] }) });
      setOpen(false); refresh(); notify(target === 'all' ? '已安排整片重新生成，剧本与旧视频已保留' : '已安排重跑，剧本与旧版产物已保留');
    } catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  return <section className="panel">
    <div className="panel-title rerun-heading">重新生成<div className="actions"><button disabled={locked || busy} onClick={() => chooseTarget(shot?.enabled ? shot.id : 'all')}>选择镜头重跑</button><button className="primary" disabled={locked || busy || !episode.shots.some(s => s.enabled)} onClick={() => chooseTarget('all')}><RefreshCw size={14} />整片重新生成</button></div></div>
    <p className="muted">{episode.creation_source ? '可以沿用创作包重新生成指定镜头或整片；修改剧情或提示词，请回到创作包保存新版本。旧版产物会保留。' : '可以重新生成指定镜头或整片，也可连同提示词一起重写。分镜故事、视觉设定、参考图和旧版产物会保留。'}</p>
    {locked && <p className="muted">任务运行中或状态尚未确认，请先等待完成、恢复或核对原任务。</p>}
    {open && <div className="rerun-form">
      <label>重跑范围<select value={target} disabled={locked || busy} onChange={e => setTarget(e.target.value)}><option value="all">整片（全部启用镜头）</option>{episode.shots.filter(s => s.enabled).map(s => <option key={s.id} value={s.id}>镜头 {s.index + 1} · {s.title}</option>)}</select></label>
      <label>生成内容<select value={scope} disabled={locked || busy} onChange={e => setScope(e.target.value)}><option value="video">仅重新生成视频（沿用已有关键帧）</option><option value="keyframes">重新生成关键帧及视频</option>{!episode.creation_source && <option value="prompts">重新生成提示词、关键帧及视频</option>}</select></label>
      {scope === 'prompts' && <p className="muted">AI 将按原分镜重新编写所选镜头的画面/视频提示词、旁白脚本及 AI 动态参数，然后开始生成。旧提示词在重跑历史中保留；高级模式的固定参数覆盖仍优先。</p>}
      <label><input type="checkbox" checked={newSeed} disabled={locked || busy} onChange={e => setNewSeed(e.target.checked)} />使用新随机种子，尝试不同结果</label>
      <small className="muted">高级参数中明确指定的种子仍优先；取消勾选可沿用种子复现。模型输出不保证一定不同。</small>
      <label>首尾帧变化要求<select value={staticFrames} disabled={locked || busy} onChange={e => setStaticFrames(e.target.value)}><option value="keep">沿用各镜头设定</option><option value="change">要求首尾帧有变化</option><option value="allow">允许静止首尾帧（有意定格）</option></select><small className="muted">仅影响所选镜头的首尾帧视频检测；图生视频不检查尾帧。</small></label>
      <div className="notice">将重跑 {selected.length} 镜，连同连续性依赖共影响 {affected.length} 镜，并重新合成整片。{scope === 'prompts' ? '分镜顺序和时长保持，所选镜头提示词重新编写。' : '剧本与提示词保持。'}旧视频不会覆盖或删除，新任务失败也能查看旧版。依赖前镜尾帧的后续镜头会重做关键帧，缺失素材会自动补齐。</div>
      <div className="actions"><button disabled={busy} onClick={() => setOpen(false)}>取消</button><button className="primary" disabled={locked || busy || !selected.length} onClick={() => void submit()}>{target === 'all' ? '确认整片重新生成' : '确认重跑'}</button></div>
    </div>}
  </section>;
}

export function RerunHistory({ episode }: { episode: Episode }) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState('');
  const [selectedShot, setSelectedShot] = useState('');
  const [selectedFilm, setSelectedFilm] = useState('');
  const history = episode.rerun_history || [];
  const films = history.map((h, index) => ({ ...h, number: index + 1 })).filter(h => h.final_video_asset_id);
  const film = films.find(h => h.id === selectedFilm) || films[films.length - 1];
  const record = history.find(h => h.id === selected) || history[history.length - 1];
  const shot = record?.shots.find(s => s.id === selectedShot) || record?.shots[0];
  if (!record) return null;
  return <>{film?.final_video_asset_id && <section className="panel previous-films" aria-label="旧版成片">
    <h2>旧版成片 · {films.length} 个版本</h2>
    <p className="muted">重新拼接或生成都会保留旧视频。生成期间或失败后，仍可在这里预览和下载。</p>
    <label>选择旧版成片<select value={film.id} onChange={e => setSelectedFilm(e.target.value)}>{[...films].reverse().map(h => <option key={h.id} value={h.id}>版本 {h.number} · {new Date(h.created_at).toLocaleString('zh-CN')}{h.final_duration != null ? ` · ${h.final_duration.toFixed(2)} 秒` : ''}</option>)}</select></label>
    <Media id={film.final_video_asset_id} kind="video" label={`旧版成片 · 版本 ${film.number}`} />
    <a className="button" href={assetUrl(film.final_video_asset_id, true)} download>下载旧版成片</a>
  </section>}<details className="panel details-panel" onToggle={e => setOpen(e.currentTarget.open)}><summary>重跑历史 · {history.length} 个旧版本</summary>{open && <div className="rerun-history-content">
    <label>重跑前的版本<select value={record.id} onChange={e => setSelected(e.target.value)}>{[...history].reverse().map((h, i) => <option key={h.id} value={h.id}>版本 {history.length - i} · {new Date(h.created_at).toLocaleString('zh-CN')} · {h.scope === 'compose' ? '重新拼接前' : h.scope === 'video' ? '视频重跑前' : h.scope === 'prompts' ? '提示词与关键帧重跑前' : '关键帧重跑前'}</option>)}</select></label>
    <label>旧版镜头<select value={shot?.id || ''} onChange={e => setSelectedShot(e.target.value)}>{record.shots.map(s => <option key={s.id} value={s.id}>{s.index + 1} · {s.title}</option>)}</select></label>
    {record.prompt_optimization && <div className="notice"><strong>AI 定向优化</strong><p>{record.prompt_optimization.summary}</p></div>}
    {shot?.prompts && <details className="preview-extra"><summary>旧版提示词与 AI 参数</summary><pre>{JSON.stringify(shot.prompts, null, 2)}</pre></details>}
    {shot && <><div className="keyframe-grid"><Media id={shot.start_frame_asset_id} kind="image" label="旧版首帧" />{shot.end_frame_asset_id && <Media id={shot.end_frame_asset_id} kind="image" label="旧版尾帧" />}</div><Media id={shot.video_asset_id} kind="video" label="旧版镜头视频" />{shot.video_asset_id && <a href={assetUrl(shot.video_asset_id, true)} download>下载旧版镜头</a>}</>}
  </div>}</details></>;
}
