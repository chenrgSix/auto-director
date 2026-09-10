import { useState, type ChangeEvent } from 'react';
import { Check, FileJson, Image, Layers3, Play, Plus, Save, ShieldCheck, Star, Trash2, Upload, Video } from 'lucide-react';
import { api, useResource } from './api';
import type { Notify } from './App';
import { JobList } from './EpisodePage';
import type { Asset, Binding, Capabilities, Job, Settings, Value, Workflow } from './types';
import { CAPABILITY_LABELS, OWNER_LABELS } from './types';
import { ParameterInput } from './DynamicParameters';
import type { WorkflowCapability, ParameterRule } from './types';
import { Badge, ErrorNotice, Loading, Media } from './ui';

export default function WorkflowsPage({ notify }: { notify: Notify }) {
  const resource = useResource<Workflow[]>('/workflows');
  const config = useResource<Settings>('/settings');
  const [selected, setSelected] = useState<string>();
  const [importing, setImporting] = useState(false);
  const [name, setName] = useState('');
  const [capability, setCapability] = useState<WorkflowCapability>('TEXT_TO_IMAGE');
  const [graph, setGraph] = useState<Record<string, unknown>>();
  const [filename, setFilename] = useState('');
  const [busy, setBusy] = useState(false);
  const current = resource.data?.find(item => item.id === selected) || resource.data?.[0];
  async function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) { notify('工作流文件不能超过 2 MiB', true); return; }
    try { setGraph(JSON.parse(await file.text())); setFilename(file.name); if (!name) setName(file.name.replace(/\.json$/i, '')); }
    catch { setGraph(undefined); notify('文件不是有效 JSON', true); }
  }
  async function importFile() {
    setBusy(true);
    try {
      const result = await api<Workflow>('/workflows/import', 'POST', { name, media_type: capability.endsWith("TO_IMAGE") ? "image" : "video", capability, workflow: graph });
      setSelected(result.id); setImporting(false); resource.refresh(); notify('工作流已导入，请检查角色绑定与依赖');
    } catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  return <><div className="section-heading"><div><span className="eyebrow">YOUR RENDER ENGINES</span><h1>工作流</h1><p>连接你熟悉的 ComfyUI 工作流，自由选择每一幅画面的生成方式。</p></div><button className="primary" onClick={() => setImporting(!importing)}><Plus size={16} />导入工作流</button></div>
    {resource.error && <ErrorNotice>{resource.error}</ErrorNotice>}
    {importing && <section className="panel import-panel"><div className="panel-title"><FileJson size={17} />导入 ComfyUI API Format JSON</div><div className="fields two"><div className="field"><label htmlFor="import-name">工作流名称</label><input id="import-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} /></div><div className="field"><label htmlFor="import-type">工作流能力</label><select id="import-type" value={capability} onChange={event => setCapability(event.target.value as WorkflowCapability)}>{Object.entries(CAPABILITY_LABELS).map(([value, label]) => <option key={value} value={value}>{label} · {value.endsWith('TO_IMAGE') ? '图像' : '视频'}</option>)}</select></div></div><label className="upload-zone"><Upload size={23} /><span>{filename || '选择 API JSON 文件'}</span><small>ComfyUI → Save (API Format) · 最大 2 MiB</small><input type="file" accept="application/json,.json" onChange={event => void selectFile(event)} /></label><div className="actions"><button onClick={() => setImporting(false)}>取消</button><button className="primary" disabled={!graph || !name.trim() || busy} onClick={() => void importFile()}>分析并导入</button></div></section>}
    {!resource.data ? <Loading /> : <div className="workflow-grid"><aside className="workflow-list">{resource.data.map(item => <button className={`workflow-card ${current?.id === item.id ? 'selected' : ''}`} key={item.id} onClick={() => setSelected(item.id)}><span className="workflow-icon">{item.media_type === 'image' ? <Image size={21} /> : <Video size={21} />}</span><div><strong>{item.name}</strong><small>{CAPABILITY_LABELS[item.capability]}</small><span className="workflow-state">{config.data?.[`default_${item.media_type}`] === item.id && <span><Star size={11} />默认</span>}{item.validation?.valid ? <span className="success-text"><Check size={11} />校验通过</span> : <span>{item.validation ? '依赖待处理' : '未校验'}</span>}</span></div></button>)}<div className="workflow-help"><Layers3 size={20} /><p>模型权重由 ComfyUI 管理。你可以更换默认工作流，也可以独立编辑每个参数。</p></div></aside>{current && <WorkflowEditor key={current.id} workflow={current} maxShotSeconds={config.data?.limits.shot_seconds.max} isDefault={config.data?.[`default_${current.media_type}`] === current.id} notify={notify} refresh={() => { resource.refresh(); config.refresh(); }} />}</div>}
  </>;
}


function WorkflowEditor({ workflow, isDefault, notify, refresh, maxShotSeconds }: { workflow: Workflow; maxShotSeconds?: number; isDefault: boolean; notify: Notify; refresh: () => void }) {
  const [name, setName] = useState(workflow.name);
  const [bindings, setBindings] = useState<Record<string, Binding>>(workflow.bindings);
  const [outputs, setOutputs] = useState(workflow.outputs);
  const [capability, setCapability] = useState<WorkflowCapability>(workflow.capability);
  const [rules, setRules] = useState<Record<string, ParameterRule>>(workflow.parameter_rules);
  const [caps, setCaps] = useState<Capabilities>(workflow.capabilities);
  const [parameters, setParameters] = useState<Record<string, Value>>(workflow.parameter_values);
  const [customRole, setCustomRole] = useState('');
  const [extraRoles, setExtraRoles] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [testPrompt, setTestPrompt] = useState('A cinematic wide shot of a lion in a prehistoric forest, natural light.');
  const [testDuration, setTestDuration] = useState(2);
  const [testAssets, setTestAssets] = useState<Record<string, string>>({});
  const [jobId, setJobId] = useState(workflow.last_test_job_id);
  const roles = [...new Set(['prompt', 'negative', 'camera_motion', 'motion_strength', 'batch', 'width', 'height', 'seed', ...(workflow.media_type === 'video' ? ['start_frame', 'end_frame', 'duration', 'fps', 'reference_video'] : ['reference_image', 'style_reference']), ...Object.keys(bindings), ...extraRoles])];
  async function perform(path: string, label: string, body?: unknown, method = 'POST') {
    setBusy(true);
    try { const result = await api<Workflow>(path, method, body); refresh(); notify(label); return result; }
    catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  async function save() { return perform(`/workflows/${workflow.id}`, '工作流已保存，请重新校验', { name, bindings, outputs, capability, capabilities: caps, parameter_values: parameters, parameter_rules: rules }, 'PATCH'); }
  async function upload(role: string, file?: File) {
    if (!file) return;
    const data = new FormData(); data.append('file', file);
    try { const asset = await api<Asset>('/assets', 'POST', data); setTestAssets(current => ({ ...current, [role]: asset.id })); notify(`${role} 已上传`); }
    catch (error) { notify((error as Error).message, true); }
  }
  async function testRun() {
    setBusy(true);
    try {
      await api(`/workflows/${workflow.id}`, 'PATCH', { name, bindings, outputs, capability, capabilities: caps, parameter_values: parameters, parameter_rules: rules });
      const job = await api<Job>(`/workflows/${workflow.id}/test-run`, 'POST', { values: { prompt: testPrompt, duration: testDuration, fps: 16 }, parameter_values: parameters, asset_bindings: testAssets });
      setJobId(job.id); refresh(); notify('试跑已进入渲染队列');
    } catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  return <div className="workflow-editor"><section className="panel"><div className="editor-heading"><div><span className="small-label">WORKFLOW PROFILE</span><h2>{workflow.name}</h2></div><div className="actions"><button disabled={busy || isDefault} onClick={() => void perform(`/workflows/${workflow.id}/default`, '已设为默认工作流')}><Star size={14} />{isDefault ? '默认工作流' : '设为默认'}</button><button className="icon-button" aria-label="删除当前工作流" disabled={busy || isDefault} onClick={() => { if (window.confirm(`删除工作流「${workflow.name}」？`)) void perform(`/workflows/${workflow.id}`, '工作流已删除', undefined, 'DELETE'); }}><Trash2 size={16} /></button></div></div><div className="field"><label htmlFor="workflow-name">名称</label><input id="workflow-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} /></div><div className="field"><label htmlFor="workflow-capability">能力类型</label><select id="workflow-capability" value={capability} onChange={event => { const value = event.target.value as WorkflowCapability; setCapability(value); setCaps({ ...caps, supports_end_frame: value === 'FIRST_LAST_TO_VIDEO' }); }}>{Object.entries(CAPABILITY_LABELS).filter(([key]) => (key.endsWith('TO_IMAGE') ? 'image' : 'video') === workflow.media_type).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div><small className="muted hash">SHA256 {workflow.workflow_hash.slice(0, 24)}… · {Object.keys(workflow.workflow).length} 个节点</small>
      {workflow.warnings.map(message => <div className="notice" key={message}>{message}</div>)}
      <div className="actions validation-row"><button disabled={busy} onClick={() => { void save().then(result => { if (result) void perform(`/workflows/${workflow.id}/validate`, '依赖检查已完成'); }); }}><ShieldCheck size={15} />校验节点与模型</button>{workflow.validation && <span className={workflow.validation.valid ? 'success-text' : 'error-text'}>{workflow.validation.valid ? '当前依赖检查通过' : `${workflow.validation.issues.length} 项需要处理`}</span>}</div>{workflow.validation?.issues.map((issue, index) => <ErrorNotice key={index}>{issue.message || issue.code}<details><summary>节点与字段详情</summary><pre>{JSON.stringify(issue, null, 2)}</pre></details></ErrorNotice>)}
    </section>
    <section className="panel"><div className="panel-title">输入与输出绑定 <span>ROLE BINDING</span></div><p className="muted">把创作角色连接到工作流中的可写字段。节点连线不会被覆盖。</p><div className="binding-table">{roles.map(role => <div className="binding-row" key={role}><label htmlFor={`binding-${role}`}>{role}</label><select id={`binding-${role}`} value={bindings[role] ? `${bindings[role].node_id}.${bindings[role].input}` : ''} onChange={event => {
      const item = workflow.parameters.find(parameter => parameter.key === event.target.value);
      const updated = { ...bindings };
      if (item) updated[role] = { node_id: item.node_id, input: item.field, transform: role === 'duration' && ['length', 'frames', 'num_frames'].includes(item.field) ? 'duration_to_frames' : 'identity', frame_multiple: 4, frame_offset: 1 };
      else delete updated[role];
      setBindings(updated);
    }}><option value="">未绑定</option>{workflow.parameters.map(item => <option key={item.key} value={item.key}>{item.node_id} · {item.class_type} / {item.field}</option>)}</select>{role === 'duration' && bindings[role] && <div className="transform-fields"><select aria-label="时长转换" value={bindings[role].transform || 'identity'} onChange={event => setBindings({ ...bindings, [role]: { ...bindings[role], transform: event.target.value } })}><option value="identity">直接使用秒数</option><option value="duration_to_frames">转换为模型帧数</option></select>{bindings[role].transform === 'duration_to_frames' && <><input aria-label="帧数倍数" type="number" min={1} max={32} value={bindings[role].frame_multiple ?? 4} onChange={event => setBindings({ ...bindings, [role]: { ...bindings[role], frame_multiple: Number(event.target.value) } })} /><select aria-label="帧数偏移" value={bindings[role].frame_offset ?? 1} onChange={event => setBindings({ ...bindings, [role]: { ...bindings[role], frame_offset: Number(event.target.value) } })}><option value={1}>+1 帧</option><option value={0}>+0 帧</option></select></>}</div>}</div>)}<div className="binding-row"><label htmlFor="output-node">输出 {workflow.media_type}</label><select id="output-node" value={outputs[workflow.media_type] || ''} onChange={event => setOutputs({ ...outputs, [workflow.media_type]: event.target.value })}><option value="">未绑定</option>{Object.entries(workflow.workflow).map(([id, node]) => <option key={id} value={id}>{id} · {node.class_type}</option>)}</select></div></div><div className="add-role"><input aria-label="新增角色名称" placeholder="添加角色，如 reference_image_1" value={customRole} onChange={event => setCustomRole(event.target.value)} /><button disabled={!/^[a-z_][a-z0-9_]*$/.test(customRole)} onClick={() => { setExtraRoles([...extraRoles, customRole]); setCustomRole(''); }}><Plus size={14} />添加</button></div>
      {workflow.media_type === 'video' && <div className="capabilities"><div className="fields two"><div className="field"><label htmlFor="max-duration">每镜最大时长</label><input id="max-duration" type="number" min={1} max={maxShotSeconds} step={0.1} value={caps.max_duration} onChange={event => setCaps({ ...caps, max_duration: Number(event.target.value) })} /></div><div className="field"><label htmlFor="low-profile">低显存工作流 ID（可选）</label><input id="low-profile" value={caps.low_memory_workflow_id || ''} onChange={event => setCaps({ ...caps, low_memory_workflow_id: event.target.value || null })} /></div></div><label className="checkbox"><input type="checkbox" checked={caps.supports_video_reference} onChange={event => setCaps({ ...caps, supports_video_reference: event.target.checked })} />支持上一镜视频参考</label></div>}
      <label className="checkbox"><input type="checkbox" checked={caps.supports_multi_reference} onChange={event => setCaps({ ...caps, supports_multi_reference: event.target.checked })} />支持多参考图（reference_image_1、reference_image_2…）</label>
    </section>
    <details className="panel details-panel"><summary>动态参数 · {workflow.parameters.length}</summary><div className="parameter-list">{workflow.parameters.map(item => <div className="field" key={item.key}><label>{item.class_type} / {item.field}<small>{item.node_id}{item.role ? ` · ${item.role}` : ''} · {OWNER_LABELS[item.owner]}</small></label><ParameterInput parameter={item} value={parameters[item.key] ?? item.default} onChange={value => setParameters({ ...parameters, [item.key]: value })} /><label className="checkbox"><input aria-label={`允许覆盖 ${item.key}`} type="checkbox" checked={(rules[item.key]?.override_policy ?? item.override_policy) === 'advanced' && (rules[item.key]?.editable ?? item.editable)} onChange={event => setRules({ ...rules, [item.key]: { ...rules[item.key], editable: event.target.checked, override_policy: event.target.checked ? 'advanced' : 'never' } })} />允许高级模式覆盖</label>{!item.role && <select aria-label={`归属 ${item.key}`} value={rules[item.key]?.owner ?? item.owner} onChange={event => setRules({ ...rules, [item.key]: { ...(rules[item.key] ?? { editable: true, override_policy: 'advanced' }), owner: event.target.value as 'ai' | 'workflow' | 'user' } })}><option value="workflow">工作流默认</option><option value="ai">AI 自动生成</option><option value="user">用户填写</option></select>}</div>)}</div></details>
    <div className="save-row"><button className="primary" disabled={busy} onClick={() => void save()}><Save size={15} />保存工作流</button></div>
    <section className="panel test-panel"><div className="panel-title"><Play size={16} />试跑工作流</div><div className="field"><label htmlFor="test-prompt">试跑提示词</label><textarea id="test-prompt" rows={3} value={testPrompt} onChange={event => setTestPrompt(event.target.value)} /></div>{workflow.media_type === 'video' && <div className="field"><label htmlFor="test-duration">试跑时长（秒，16 FPS）</label><input id="test-duration" type="number" min={1} max={caps.max_duration} step={0.1} value={testDuration} onChange={event => setTestDuration(Number(event.target.value))} /></div>}{Object.keys(bindings).filter(role => ['start_frame', 'end_frame', 'reference_image', 'style_reference', 'reference_video'].includes(role) || role.startsWith('reference_image_')).map(role => <label key={role} className="file-field"><span>{role} {testAssets[role] && <Check size={14} />}</span><input type="file" accept={role === 'reference_video' ? 'video/*' : 'image/png,image/jpeg,image/webp'} onChange={event => void upload(role, event.target.files?.[0])} /></label>)}<button disabled={busy || !testPrompt.trim()} onClick={() => void testRun()}><Play size={15} />保存并试跑</button>{jobId && <TestJob id={jobId} kind={workflow.media_type} notify={notify} />}</section>
  </div>;
}

function TestJob({ id, kind, notify }: { id: string; kind: 'image' | 'video'; notify: Notify }) {
  const job = useResource<Job>(`/jobs/${id}`, 1500);
  if (!job.data) return job.error ? <ErrorNotice>{job.error}</ErrorNotice> : <Loading />;
  return <div className="test-result"><Badge status={job.data.status} /><JobList jobs={[job.data]} notify={notify} onChange={job.refresh} />{['QUEUED', 'RUNNING'].includes(job.data.status) && <button onClick={() => { void api(`/jobs/${id}/cancel`, 'POST').then(job.refresh).catch(error => notify(error.message, true)); }}>取消试跑</button>}{job.data.output_asset_ids.map(asset => <Media key={asset} id={asset} kind={kind} label="试跑输出" />)}</div>;
}
