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
      await api(`/episodes/${episode.id}/workflows`, 'PATCH', draft);
      setDraft(undefined); onSaved(); notify('工作流绑定已更新，点击“继续生成”按新配置重新规划');
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="panel" aria-label="短片工作流绑定">
    <div className="panel-title">本片使用的工作流</div>
    {workflows.error && <ErrorNotice>{workflows.error}</ErrorNotice>}
    {!draft ? <>
      {FIELDS.map(([key, label]) => <p className="muted" key={key}>{label}：{workflows.data?.find(item => item.id === episode[key])?.name || episode[key] || '未配置'}</p>)}
      <button disabled={busy || locked || !workflows.data} onClick={() => { setError(undefined); setDraft({ expected_version: episode.version, image_workflow_id: episode.image_workflow_id, reference_workflow_id: episode.reference_workflow_id, video_workflow_id: episode.video_workflow_id }); }}>更换工作流</button>
    </> : <>
      <p className="muted">更换后将重置当前计划、视觉设定和生成结果，保留创作要求。旧计划、作业与素材保留在历史记录中；已换走工作流的高级参数覆盖会移除。保存后点击“继续生成”重新规划。</p>
      <div className="fields two">{FIELDS.map(([key, label]) => <div className="field" key={key}>
        <label htmlFor={`episode-${key}`}>{label}</label>
        <select id={`episode-${key}`} disabled={busy || locked} value={draft[key] || ''} onChange={event => setDraft({ ...draft, [key]: event.target.value })}>
          <option value="">请选择工作流</option>
          {workflows.data?.filter(item => key === 'reference_workflow_id' ? item.capability === 'TEXT_TO_IMAGE' : item.media_type === (key === 'video_workflow_id' ? 'video' : 'image')).map(item => <option key={item.id} value={item.id} disabled={!!item.binding_issues?.length}>{item.name} · {CAPABILITY_LABELS[item.capability]}{item.binding_issues?.length ? '（绑定待完成）' : ''}</option>)}
        </select>
      </div>)}</div>
      <p className="muted">绑定完整即可选择。模型与节点将在生成前检查；更换本片不会改变全局默认项。</p>
      {error && <ErrorNotice>{error}</ErrorNotice>}
      <div className="actions">
        <button disabled={busy} onClick={() => setDraft(undefined)}>取消更换</button>
        <button disabled={busy || locked || !workflows.data} onClick={() => void defaults()}>使用当前默认</button>
        <button className="primary" disabled={busy || locked || !changed || FIELDS.some(([key]) => !draft[key])} onClick={() => void save()}>保存绑定并重置计划</button>
      </div>
    </>}
    {locked && <p className="muted">请先等待生成结束，或停止并核对未完成作业后再更换。</p>}
  </section>;
}
