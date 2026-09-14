import { useState, type ChangeEvent } from 'react';
import { Check, FileJson, Image, Layers3, Plus, Star, Upload, Video } from 'lucide-react';
import { api, useResource } from './api';
import type { Notify } from './App';
import type { Settings, Workflow } from './types';
import { CAPABILITY_LABELS } from './types';
import { WorkflowEditor } from './WorkflowEditor';
import type { WorkflowCapability } from './types';
import { ErrorNotice, Loading } from './ui';
import { WorkflowAI, type Recognition } from './WorkflowAI';

export default function WorkflowsPage({ notify }: { notify: Notify }) {
  const resource = useResource<Workflow[]>('/workflows');
  const config = useResource<Settings>('/settings');
  const [editorDirty, setEditorDirty] = useState(false);
  const [selected, setSelected] = useState<string>();
  const [importing, setImporting] = useState(false);
  const [name, setName] = useState('');
  const [capability, setCapability] = useState<WorkflowCapability | ''>('');
  const [recognized, setRecognized] = useState<Recognition>();
  const [profile, setProfile] = useState<Pick<Workflow, 'bindings' | 'outputs' | 'capabilities' | 'parameter_rules'>>();
  const [graph, setGraph] = useState<Record<string, unknown>>();
  const [filename, setFilename] = useState('');
  const [busy, setBusy] = useState(false);
  const current = resource.data?.find(item => item.id === selected) || resource.data?.[0];
  async function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setGraph(undefined); setProfile(undefined); setRecognized(undefined); setCapability(''); setFilename('');
    if (file.size > 2 * 1024 * 1024) { notify('工作流文件不能超过 2 MiB', true); return; }
    setBusy(true);
    try {
      const parsed = JSON.parse(await file.text());
      if (parsed?.workflow && !Array.isArray(parsed.workflow) && Object.hasOwn(CAPABILITY_LABELS, parsed.capability)) {
        setGraph(parsed.workflow); setName(parsed.name || file.name.replace(/\.json$/i, '')); setCapability(parsed.capability);
        setProfile({ bindings: parsed.bindings, outputs: parsed.outputs, capabilities: parsed.capabilities, parameter_rules: parsed.parameter_rules });
      } else {
        setGraph(parsed); setName(file.name.replace(/\.json$/i, ''));
      }
      setFilename(file.name);
    } catch (error) { notify(error instanceof SyntaxError ? '文件不是有效 JSON' : (error as Error).message, true); }
    finally { setBusy(false); }
  }
  async function importFile() {
    if (editorDirty && !window.confirm('当前工作流有未保存修改，放弃修改并导入新工作流？')) return;
    setBusy(true);
    try {
      const result = await api<Workflow>('/workflows/import', 'POST', { ...profile, name, capability, workflow: graph, ...(recognized ? {bindings: recognized.bindings, outputs: recognized.outputs} : {}) });
      setEditorDirty(false); setSelected(result.id); setImporting(false); resource.refresh(); notify('工作流已导入。可选择 AI 识别或手动绑定，依赖检查可稍后执行。');
    } catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  return <><div className="section-heading"><div><span className="eyebrow">YOUR RENDER ENGINES</span><h1>工作流</h1><p>上传并保存工作流，再选择 AI 识别或手动绑定。</p></div><button className="primary" onClick={() => setImporting(!importing)}><Plus size={16} />导入工作流</button></div>
    {resource.error && <ErrorNotice>{resource.error}</ErrorNotice>}
    {importing && <section className="panel import-panel"><div className="panel-title"><FileJson size={17} />导入工作流</div><p className="muted">上传 ComfyUI API JSON 或项目配套 profile JSON 即可导入，不等待模型或 ComfyUI。</p><label className="upload-zone"><Upload size={23} /><span>{busy ? '正在导入…' : filename || '选择工作流文件'}</span><small>ComfyUI → Save (API Format) · 最大 2 MiB</small><input disabled={busy} type="file" accept="application/json,.json" onChange={event => void selectFile(event)} /></label>{graph && <><div className="fields two"><div className="field"><label htmlFor="import-name">工作流名称</label><input id="import-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} /></div><div className="field"><label htmlFor="import-type">生成用途</label><select id="import-type" value={capability} onChange={event => { setCapability(event.target.value as WorkflowCapability | ''); setRecognized(undefined); setProfile(undefined); }}><option value="">请选择用途</option>{Object.entries(CAPABILITY_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div></div>{profile && <p className="success-text">已读取配套用途、输入输出绑定和生成限制。直接导入后可检查依赖。</p>}<WorkflowAI key={`${filename}:${capability}`} graph={graph} capability={capability || undefined} disabled={busy} onApply={proposal => { setCapability(proposal.capability); setRecognized(proposal); notify('已应用 AI 建议，点击直接导入保存。'); }} />{recognized && <p className="success-text">AI 建议已应用，导入时一起保存。</p>}</>}<div className="actions"><button disabled={busy} onClick={() => setImporting(false)}>取消</button><button className="primary" disabled={!graph || !name.trim() || busy || !capability} onClick={() => void importFile()}>直接导入</button></div></section>}
    {!resource.data ? <Loading /> : <div className="workflow-grid"><aside className="workflow-list">{resource.data.map(item => <button className={`workflow-card ${current?.id === item.id ? 'selected' : ''}`} key={item.id} onClick={() => { if (item.id !== current?.id && (!editorDirty || window.confirm('当前工作流有未保存修改，放弃修改并切换？'))) { setEditorDirty(false); setSelected(item.id); } }}><span className="workflow-icon">{item.media_type === 'image' ? <Image size={21} /> : <Video size={21} />}</span><div><strong>{item.name}</strong><small>{CAPABILITY_LABELS[item.capability]}</small><span className="workflow-state">{config.data?.[`default_${item.media_type}`] === item.id && <span><Star size={11} />默认</span>}{item.binding_issues?.length ? <span>输入输出待确认</span> : item.validation?.valid ? <span className="success-text"><Check size={11} />依赖检查通过</span> : <span>{item.validation ? '依赖待处理' : '依赖未检查'}</span>}</span></div></button>)}<div className="workflow-help"><Layers3 size={20} /><p>模型权重由 ComfyUI 管理。你可以更换默认工作流，也可以独立编辑每个参数。</p></div></aside>{current && <WorkflowEditor key={current.id} workflow={current} onDirtyChange={setEditorDirty} maxShotSeconds={config.data?.limits.shot_seconds.max} isDefault={config.data?.[`default_${current.media_type}`] === current.id} notify={notify} refresh={() => { resource.refresh(); config.refresh(); }} />}</div>}
  </>;
}
