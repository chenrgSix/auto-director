import { useState } from 'react';
import { api, useResource } from './api';
import type { Notify } from './App';
import { CAPABILITY_LABELS, type Episode, type Settings, type Workflow } from './types';
import { ErrorNotice } from './ui';

const FIELDS = [
  ['image_workflow_id', '关键帧工作流'],
  ['reference_workflow_id', '参考图工作流（文生图）'],
  ['video_workflow_id', '视频工作流'],
] as const;
type Selection = Pick<Episode, typeof FIELDS[number][0]>;
type Draft = Selection & { expected_version: number };

export function EpisodeWorkflows({ episode, busy, locked, setBusy, onSaved, notify }: {
  episode: Episode; busy: boolean; locked: boolean; setBusy: (value: boolean) => void;
  onSaved: () => void; notify: Notify;
}) {
  const workflows = useResource<Workflow[]>('/workflows');
  const [draft, setDraft] = useState<Draft>();
  const [error, setError] = useState<string>();
  const sequence = draft ? workflows.data?.find(item => item.id === draft.video_workflow_id)?.capability === 'REFERENCE_SEQUENCE_TO_VIDEO' : episode.production_mode === 'reference_sequence';
  const fields = FIELDS.filter(([key]) => !sequence || key !== 'image_workflow_id');
  const changed = draft && FIELDS.some(([key]) => draft[key] !== episode[key]);
  async function defaults() {
    if (!draft) return;
    setBusy(true); setError(undefined);
    try {
      const [config, profiles] = await Promise.all([api<Settings>('/settings'), api<Workflow[]>('/workflows')]);
      const preferred = (media: 'image' | 'video') => {
        const profile = profiles.find(item => item.id === config[`default_${media}`]);
        return profile ? config.default_capabilities[profile.capability] || profile.id
          : Object.entries(config.default_capabilities).find(([key]) => key.endsWith(media === 'image' ? 'TO_IMAGE' : 'TO_VIDEO'))?.[1] || '';
      };
      const image = preferred('image');
      const reference = config.default_capabilities.TEXT_TO_IMAGE
        || (profiles.find(item => item.id === image)?.capability === 'TEXT_TO_IMAGE' ? image : '');
      workflows.refresh();
      setDraft({ ...draft, image_workflow_id: image, video_workflow_id: preferred('video'), reference_workflow_id: reference });
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  async function save() {
    if (!draft) return;
    setBusy(true); setError(undefined);
    try {
      const saved = await api<Episode>(`/episodes/${episode.id}/workflows`, 'PATCH', { ...draft, ...(sequence ? { image_workflow_id: null } : {}) });
      setDraft(undefined); onSaved();
      notify(saved.refresh_workflow_budget ? '工作流已更换，剧本和已有素材已保留，点击继续生成补齐缺失部分' : saved.shots.length ? '工作流已更换，故事与分镜已保留，请检查后确认生成' : '工作流已更新，可继续准备短片');
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  async function restore() {
    setBusy(true); setError(undefined);
    try {
      await api(`/episodes/${episode.id}/workflows/restore`, 'POST', {
        expected_version: episode.version, history_revision: episode.recoverable_workflow_revision,
      });
      onSaved(); notify('剧本和已有素材已恢复，保留当前工作流，尚未开始生成');
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="panel" aria-label="短片工作流绑定">
    <div className="panel-title">本片使用的工作流</div>
    {workflows.error && <ErrorNotice>{workflows.error}</ErrorNotice>}
    {!draft ? <>
      {fields.map(([key, label]) => <p className="muted" key={key}>{label}：{workflows.data?.find(item => item.id === episode[key])?.name || episode[key] || '未配置'}</p>)}
      <button disabled={busy || locked || !workflows.data} onClick={() => { setError(undefined); setDraft({ expected_version: episode.version, image_workflow_id: episode.image_workflow_id, reference_workflow_id: episode.reference_workflow_id, video_workflow_id: episode.video_workflow_id }); }}>更换工作流</button>
    </> : <>
      <p className="muted">更换工作流会保留剧本、分镜时长、提示词，以及已生成的参考图、关键帧和视频。保存不会自动开始生成。</p>
      <p className="muted">新工作流用于后续缺失部分；已有结果需要重新生成时，请在对应镜头点击重试。继续生成前会检查模型、时长和显存限制。已换走工作流的专属参数不会套用到新工作流。</p>
      <div className="fields two">{fields.map(([key, label]) => <div className="field" key={key}>
        <label htmlFor={`episode-${key}`}>{label}</label>
        <select id={`episode-${key}`} disabled={busy || locked} value={draft[key] || ''} onChange={event => setDraft({ ...draft, [key]: event.target.value })}>
          <option value="">请选择工作流</option>
          {workflows.data?.filter(item => key === 'reference_workflow_id' ? item.capability === 'TEXT_TO_IMAGE' : item.media_type === (key === 'video_workflow_id' ? 'video' : 'image')).map(item => <option key={item.id} value={item.id} disabled={!!item.binding_issues?.length}>{item.name} · {CAPABILITY_LABELS[item.capability]}{item.binding_issues?.length ? '（绑定待完成）' : ''}</option>)}
        </select>
      </div>)}</div>
      <p className="muted">绑定完整即可选择。模型与节点将在生成前检查；更换本片不会改变全局默认项。</p>
      <div className="actions">
        <button disabled={busy} onClick={() => setDraft(undefined)}>取消更换</button>
        <button disabled={busy || locked || !workflows.data} onClick={() => void defaults()}>使用当前默认</button>
        <button className="primary" disabled={busy || locked || !changed || fields.some(([key]) => !draft[key])} onClick={() => void save()}>保存工作流</button>
      </div>
    </>}
    {!draft && episode.recoverable_workflow_revision != null && <div className="notice"><p>更换前的剧本和素材记录仍保留在历史中，可以恢复到当前短片并继续使用当前工作流。</p><button disabled={busy || locked} onClick={() => void restore()}>恢复剧本和已有素材</button></div>}
    {episode.refresh_workflow_budget && <p className="muted">剧本和已有素材已保留。继续生成会沿用已有结果，只补齐缺失部分；重试镜头会重新生成对应结果。</p>}
    {error && <ErrorNotice>{error}</ErrorNotice>}
    {locked && <p className="muted">请先等待生成结束，或停止并核对未完成作业后再更换。</p>}
  </section>;
}
