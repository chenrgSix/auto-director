import { useRef, useState, type ChangeEvent } from 'react';
import { Check, FileJson, Image, Layers3, Play, Plus, Save, Star, Trash2, Upload, Video } from 'lucide-react';
import { api, useResource } from './api';
import type { Notify } from './App';
import { JobList } from './EpisodePage';
import type { Asset, Binding, Capabilities, Job, Settings, Value, Workflow } from './types';
import { CAPABILITY_LABELS } from './types';
import { ParameterInput } from './DynamicParameters';
import type { WorkflowCapability, ParameterRule } from './types';
import { Badge, ErrorNotice, Loading, Media } from './ui';
import { BindingGuide, WorkflowReadiness, bindingKey, parameterLabel, roleLabel, availableRoles } from './WorkflowBindings';
import { WorkflowAI, mergeBindings, type Recognition } from './WorkflowAI';

export default function WorkflowsPage({ notify }: { notify: Notify }) {
  const resource = useResource<Workflow[]>('/workflows');
  const config = useResource<Settings>('/settings');
  const [selected, setSelected] = useState<string>();
  const [importing, setImporting] = useState(false);
  const [name, setName] = useState('');
  const [capability, setCapability] = useState<WorkflowCapability | ''>('');
  const [recognized, setRecognized] = useState<Recognition>();
  const [graph, setGraph] = useState<Record<string, unknown>>();
  const [filename, setFilename] = useState('');
  const [busy, setBusy] = useState(false);
  const current = resource.data?.find(item => item.id === selected) || resource.data?.[0];
  async function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setGraph(undefined); setRecognized(undefined); setCapability(''); setFilename('');
    if (file.size > 2 * 1024 * 1024) { notify('工作流文件不能超过 2 MiB', true); return; }
    setBusy(true);
    try {
      const parsed = JSON.parse(await file.text());
      setGraph(parsed); setFilename(file.name); setName(file.name.replace(/\.json$/i, ''));
    } catch (error) { notify(error instanceof SyntaxError ? '文件不是有效 JSON' : (error as Error).message, true); }
    finally { setBusy(false); }
  }
  async function importFile() {
    setBusy(true);
    try {
      const result = await api<Workflow>('/workflows/import', 'POST', { name, capability, workflow: graph, ...(recognized ? {bindings: recognized.bindings, outputs: recognized.outputs} : {}) });
      setSelected(result.id); setImporting(false); resource.refresh(); notify('工作流已导入。可选择 AI 识别或手动绑定，依赖检查可稍后执行。');
    } catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  return <><div className="section-heading"><div><span className="eyebrow">YOUR RENDER ENGINES</span><h1>工作流</h1><p>上传并保存工作流，再选择 AI 识别或手动绑定。</p></div><button className="primary" onClick={() => setImporting(!importing)}><Plus size={16} />导入工作流</button></div>
    {resource.error && <ErrorNotice>{resource.error}</ErrorNotice>}
    {importing && <section className="panel import-panel"><div className="panel-title"><FileJson size={17} />导入工作流</div><p className="muted">上传 ComfyUI API 格式 JSON 并选择用途即可导入，不等待模型或 ComfyUI。</p><label className="upload-zone"><Upload size={23} /><span>{busy ? '正在导入…' : filename || '选择工作流文件'}</span><small>ComfyUI → Save (API Format) · 最大 2 MiB</small><input disabled={busy} type="file" accept="application/json,.json" onChange={event => void selectFile(event)} /></label>{graph && <><div className="fields two"><div className="field"><label htmlFor="import-name">工作流名称</label><input id="import-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} /></div><div className="field"><label htmlFor="import-type">生成用途</label><select id="import-type" value={capability} onChange={event => { setCapability(event.target.value as WorkflowCapability | ''); setRecognized(undefined); }}><option value="">请选择用途</option>{Object.entries(CAPABILITY_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div></div><WorkflowAI key={`${filename}:${capability}`} graph={graph} capability={capability || undefined} disabled={busy} onApply={proposal => { setCapability(proposal.capability); setRecognized(proposal); notify('已应用 AI 建议，点击直接导入保存。'); }} />{recognized && <p className="success-text">AI 建议已应用，导入时一起保存。</p>}</>}<div className="actions"><button disabled={busy} onClick={() => setImporting(false)}>取消</button><button className="primary" disabled={!graph || !name.trim() || busy || !capability} onClick={() => void importFile()}>直接导入</button></div></section>}
    {!resource.data ? <Loading /> : <div className="workflow-grid"><aside className="workflow-list">{resource.data.map(item => <button className={`workflow-card ${current?.id === item.id ? 'selected' : ''}`} key={item.id} onClick={() => setSelected(item.id)}><span className="workflow-icon">{item.media_type === 'image' ? <Image size={21} /> : <Video size={21} />}</span><div><strong>{item.name}</strong><small>{CAPABILITY_LABELS[item.capability]}</small><span className="workflow-state">{config.data?.[`default_${item.media_type}`] === item.id && <span><Star size={11} />默认</span>}{item.binding_issues?.length ? <span>输入输出待确认</span> : item.validation?.valid ? <span className="success-text"><Check size={11} />依赖检查通过</span> : <span>{item.validation ? '依赖待处理' : '依赖未检查'}</span>}</span></div></button>)}<div className="workflow-help"><Layers3 size={20} /><p>模型权重由 ComfyUI 管理。你可以更换默认工作流，也可以独立编辑每个参数。</p></div></aside>{current && <WorkflowEditor key={current.id} workflow={current} maxShotSeconds={config.data?.limits.shot_seconds.max} isDefault={config.data?.[`default_${current.media_type}`] === current.id} notify={notify} refresh={() => { resource.refresh(); config.refresh(); }} />}</div>}
  </>;
}


function WorkflowEditor({ workflow, isDefault, notify, refresh, maxShotSeconds }: { workflow: Workflow; maxShotSeconds?: number; isDefault: boolean; notify: Notify; refresh: () => void }) {
  const [saved, setSaved] = useState(workflow);
  const [name, setName] = useState(workflow.name);
  const [bindings, setBindings] = useState<Record<string, Binding>>(workflow.bindings);
  const [outputs, setOutputs] = useState(workflow.outputs);
  const [capability, setCapability] = useState<WorkflowCapability>(workflow.capability);
  const [rules, setRules] = useState<Record<string, ParameterRule>>(workflow.parameter_rules);
  const [caps, setCaps] = useState<Capabilities>(workflow.capabilities);
  const [parameters, setParameters] = useState<Record<string, Value>>(workflow.parameter_values);
  const [busy, setBusy] = useState(false);
  const [testPrompt, setTestPrompt] = useState('A cinematic wide shot of a lion in a prehistoric forest, natural light.');
  const [testDuration, setTestDuration] = useState(2);
  const [testAssets, setTestAssets] = useState<Record<string, string>>({});
  const [jobId, setJobId] = useState(workflow.last_test_job_id);
  const [advanced, setAdvanced] = useState(false);
  const advancedPanel = useRef<HTMLDetailsElement>(null);
  const roles = Object.keys(bindings).filter(role => availableRoles(capability).includes(role) || (capability !== 'TEXT_TO_IMAGE' && role.startsWith('reference_image_')));
  const roleOptions = availableRoles(capability);
  const dirty = Object.entries({name, bindings, outputs, capability, capabilities: caps, parameter_values: parameters, parameter_rules: rules}).some(([key, value]) => JSON.stringify(value) !== JSON.stringify(saved[key as keyof Workflow]));
  function acceptWorkflow(result: Workflow) {
    setSaved(result); setName(result.name); setBindings(result.bindings); setOutputs(result.outputs);
    setCapability(result.capability); setCaps(result.capabilities); setRules(result.parameter_rules); setParameters(result.parameter_values);
  }
  function showAdvanced() { setAdvanced(true); requestAnimationFrame(() => advancedPanel.current?.scrollIntoView({behavior: 'smooth', block: 'start'})); }
  async function perform(path: string, label: string, body?: unknown, method = 'POST') {
    setBusy(true);
    try { const result = await api<Workflow>(path, method, body); if (result?.id === workflow.id && result.bindings) acceptWorkflow(result); refresh(); notify(label); return result; }
    catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  async function save() { return perform(`/workflows/${workflow.id}`, '工作流已保存，请重新校验', { name, bindings, outputs, capability, capabilities: caps, parameter_values: parameters, parameter_rules: rules }, 'PATCH'); }
  async function setDefault() {
    setBusy(true);
    try {
      if (dirty) acceptWorkflow(await api<Workflow>(`/workflows/${workflow.id}`, 'PATCH', { name, bindings, outputs, capability, capabilities: caps, parameter_values: parameters, parameter_rules: rules }));
      await api<Settings>(`/workflows/${workflow.id}/default`, 'POST');
      notify('已设为默认工作流，新建短片时生效');
    } catch (error) { notify((error as Error).message, true); }
    finally { refresh(); setBusy(false); }
  }
  async function upload(role: string, file?: File) {
    if (!file) return;
    const data = new FormData(); data.append('file', file);
    try { const asset = await api<Asset>('/assets', 'POST', data); setTestAssets(current => ({ ...current, [role]: asset.id })); notify(`${roleLabel(role)}已上传`); }
    catch (error) { notify((error as Error).message, true); }
  }
  async function testRun() {
    setBusy(true);
    try {
      acceptWorkflow(await api<Workflow>(`/workflows/${workflow.id}`, 'PATCH', { name, bindings, outputs, capability, capabilities: caps, parameter_values: parameters, parameter_rules: rules }));
      const checked = await api<Workflow>(`/workflows/${workflow.id}/validate`, 'POST');
      acceptWorkflow(checked);
      if (!checked.validation?.valid) { refresh(); notify('请先处理输入输出或模型与节点问题。', true); return; }
      const job = await api<Job>(`/workflows/${workflow.id}/test-run`, 'POST', { values: { prompt: testPrompt, duration: testDuration, fps: 16 }, asset_bindings: testAssets });
      setJobId(job.id); refresh(); notify('试跑已进入渲染队列');
    } catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  return <div className="workflow-editor"><section className="panel"><div className="editor-heading"><div><span className="small-label">WORKFLOW PROFILE</span><h2>{workflow.name}</h2></div><div className="actions"><button disabled={busy || isDefault} onClick={() => void setDefault()}><Star size={14} />{isDefault ? '默认工作流' : dirty ? '保存并设为默认' : '设为默认'}</button><button className="icon-button" aria-label="删除当前工作流" disabled={busy || isDefault} onClick={() => { if (window.confirm(`删除工作流「${workflow.name}」？`)) void perform(`/workflows/${workflow.id}`, '工作流已删除', undefined, 'DELETE'); }}><Trash2 size={16} /></button></div></div><p className="muted">绑定完整即可设为默认，用于新建短片；生成前仍会检查模型与节点。</p><div className="field"><label htmlFor="workflow-name">名称</label><input id="workflow-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} /></div><div className="field"><label htmlFor="workflow-capability">生成用途</label><select id="workflow-capability" value={capability} onChange={event => { const value = event.target.value as WorkflowCapability; setCapability(value); setCaps({ ...caps, supports_end_frame: value === 'FIRST_LAST_TO_VIDEO' }); if (value === 'IMAGE_TO_VIDEO') { const next = {...bindings}; delete next.end_frame; setBindings(next); } }}>{Object.entries(CAPABILITY_LABELS).filter(([key]) => (key.endsWith('TO_IMAGE') ? 'image' : 'video') === workflow.media_type).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div></section>
    <section className="panel"><div className="panel-title">配置方式</div><WorkflowAI key={`${workflow.id}:${capability}`} graph={workflow.workflow} capability={capability} disabled={busy} onApply={proposal => { const next = mergeBindings(bindings, proposal.bindings); setBindings(next); const updatedRules = {...rules}; for (const [role, binding] of Object.entries(next)) { const key = bindingKey(binding); if (!bindings[role] && updatedRules[key]?.owner) { const rule = updatedRules[key]; updatedRules[key] = {editable: rule.editable, override_policy: rule.override_policy}; } } setRules(updatedRules); setOutputs(current => ({...proposal.outputs, ...Object.fromEntries(Object.entries(current).filter(([, value]) => value))})); notify('AI 建议已填入未绑定项，请保存工作流。'); }} /><button onClick={showAdvanced}>手动配置</button></section>
    <BindingGuide workflow={{...workflow, capabilities: caps}} capability={capability} bindings={bindings} outputs={outputs} onAdvanced={showAdvanced} />
    <WorkflowReadiness workflow={workflow} dirty={dirty} busy={busy} onCheck={() => { void save().then(result => { if (result) void perform(`/workflows/${workflow.id}/validate`, '依赖检查已完成'); }); }} onAdvanced={showAdvanced} />
    <details ref={advancedPanel} className="panel details-panel workflow-advanced" open={advanced} onToggle={event => setAdvanced(event.currentTarget.open)}><summary>参数与输出 · {workflow.parameters.length} 个实际字段</summary><p className="muted">每行对应此工作流中真实存在的可写字段。用途决定生成时由谁填写；未指定用途的字段使用工作流默认值。</p>
    <div className="parameter-list">{workflow.parameters.map(item => {
      const role = Object.entries(bindings).find(([, binding]) => bindingKey(binding) === item.key)?.[0] || '';
      const binding = role ? bindings[role] : undefined;
      const numeric = ['integer', 'number'].includes(item.type);
      const choices = [...new Set([...roleOptions, ...(role ? [role] : [])])].filter(value => {
        if (value === role) return true;
        if (item.asset_kind) return ['start_frame', 'end_frame', 'reference_image', 'style_reference', 'reference_video'].includes(value);
        return numeric === ['duration', 'fps', 'width', 'height', 'batch', 'seed', 'motion_strength'].includes(value);
      });
      return <div className="field workflow-parameter" key={item.key}><label>{parameterLabel(workflow, item.key)}</label>
        <label className="parameter-purpose">生成用途<select aria-label={`用途 ${item.key}`} value={role} onChange={event => {
          const next = {...bindings};
          if (role) delete next[role];
          if (event.target.value) next[event.target.value] = {node_id: item.node_id, input: item.field, transform: 'identity', frame_multiple: 4, frame_offset: 1};
          setBindings(next);
          if (rules[item.key]?.owner) setRules({...rules, [item.key]: {editable: rules[item.key].editable, override_policy: rules[item.key].override_policy}});
        }}><option value="">工作流默认</option>{choices.map(value => <option key={value} value={value} disabled={!!bindings[value] && value !== role}>{roleLabel(value)}{bindings[value] && value !== role ? '（其他字段已使用）' : ''}</option>)}</select></label>
        <ParameterInput parameter={item} value={parameters[item.key] ?? item.default} onChange={value => setParameters({...parameters, [item.key]: value})} />
        {role === 'duration' && binding && <div className="transform-fields"><select aria-label="时长转换" value={binding.transform || 'identity'} onChange={event => setBindings({...bindings, duration: {...binding, transform: event.target.value}})}><option value="identity">直接使用秒数</option><option value="duration_to_frames">转换为模型帧数</option></select>{binding.transform === 'duration_to_frames' && <><input aria-label="帧数倍数" type="number" min={1} max={32} value={binding.frame_multiple ?? 4} onChange={event => setBindings({...bindings, duration: {...binding, frame_multiple: Number(event.target.value)}})} /><select aria-label="帧数偏移" value={binding.frame_offset ?? 1} onChange={event => setBindings({...bindings, duration: {...binding, frame_offset: Number(event.target.value)}})}><option value={1}>+1 帧</option><option value={0}>+0 帧</option></select></>}</div>}
        <label className="checkbox"><input aria-label={`允许覆盖 ${item.key}`} type="checkbox" checked={(rules[item.key]?.override_policy ?? item.override_policy) === 'advanced' && (rules[item.key]?.editable ?? item.editable)} onChange={event => setRules({...rules, [item.key]: {...rules[item.key], editable: event.target.checked, override_policy: event.target.checked ? 'advanced' : 'never'}})} />允许创作时高级覆盖</label>
        {!role && !item.asset_kind && <select aria-label={`归属 ${item.key}`} value={rules[item.key]?.owner ?? (item.role ? 'workflow' : item.owner)} onChange={event => setRules({...rules, [item.key]: {...(rules[item.key] ?? {editable: true, override_policy: 'advanced'}), owner: event.target.value as 'ai' | 'workflow' | 'user'}})}><option value="workflow">工作流默认</option><option value="ai">AI 自动生成</option><option value="user">用户填写</option></select>}
        {role && <small className="muted">用途已指定为{roleLabel(role)}，生成时自动填写；此处显示模板默认值。</small>}
      </div>;
    })}</div>
    <div className="field"><label htmlFor="output-node">生成结果 · 输出节点</label><select id="output-node" value={outputs[workflow.media_type] || ''} onChange={event => setOutputs({...outputs, [workflow.media_type]: event.target.value})}><option value="">请选择输出节点</option>{Object.entries(workflow.workflow).map(([id, node]) => <option key={id} value={id}>{node._meta?.title || node.class_type}（{id}）</option>)}</select></div>
    {workflow.media_type === 'video' && <div className="capabilities"><div className="fields two"><div className="field"><label htmlFor="max-duration">每镜最大时长</label><input id="max-duration" type="number" min={1} max={maxShotSeconds} step={0.1} value={caps.max_duration} onChange={event => setCaps({...caps, max_duration: Number(event.target.value)})} /></div><div className="field"><label htmlFor="low-profile">低显存工作流 ID（可选）</label><input id="low-profile" value={caps.low_memory_workflow_id || ''} onChange={event => setCaps({...caps, low_memory_workflow_id: event.target.value || null})} /></div></div>{bindings.reference_video && <label className="checkbox"><input type="checkbox" checked={caps.supports_video_reference} onChange={event => setCaps({...caps, supports_video_reference: event.target.checked})} />需要上一镜视频参考</label>}</div>}
    {capability === 'IMAGE_TO_IMAGE' && Object.keys(bindings).some(role => role.startsWith('reference_image_')) && <label className="checkbox"><input type="checkbox" checked={caps.supports_multi_reference} onChange={event => setCaps({...caps, supports_multi_reference: event.target.checked})} />支持多参考图</label>}
    <small className="muted hash">SHA256 {workflow.workflow_hash.slice(0, 24)}… · {Object.keys(workflow.workflow).length} 个节点</small></details>
    <div className="save-row"><span className="muted">{dirty ? '有未保存的修改' : '配置已保存'}</span><button className="primary" disabled={busy} onClick={() => void save()}><Save size={15} />保存工作流</button></div>
    <section className="panel test-panel"><div className="panel-title"><Play size={16} />试跑验证</div><p className="muted">用测试描述和素材验证生成结果。试跑会使用连接的 ComfyUI。</p><div className="field"><label htmlFor="test-prompt">测试画面描述</label><textarea id="test-prompt" rows={3} value={testPrompt} onChange={event => setTestPrompt(event.target.value)} /></div>{workflow.media_type === 'video' && <div className="field"><label htmlFor="test-duration">试跑时长（秒，16 FPS）</label><input id="test-duration" type="number" min={1} max={caps.max_duration} step={0.1} value={testDuration} onChange={event => setTestDuration(Number(event.target.value))} /></div>}{roles.filter(role => bindings[role]).filter(role => ['start_frame', 'end_frame', 'reference_image', 'style_reference', 'reference_video'].includes(role) || role.startsWith('reference_image_')).map(role => <label key={role} className="file-field"><span>{roleLabel(role)} {testAssets[role] && <Check size={14} />}</span><input type="file" accept={role === 'reference_video' ? 'video/*' : 'image/png,image/jpeg,image/webp'} onChange={event => void upload(role, event.target.files?.[0])} /></label>)}<button disabled={busy || !testPrompt.trim()} onClick={() => void testRun()}><Play size={15} />保存并试跑</button>{jobId && <TestJob id={jobId} kind={workflow.media_type} notify={notify} />}</section>
  </div>;
}

function TestJob({ id, kind, notify }: { id: string; kind: 'image' | 'video'; notify: Notify }) {
  const job = useResource<Job>(`/jobs/${id}`, 1500);
  if (!job.data) return job.error ? <ErrorNotice>{job.error}</ErrorNotice> : <Loading />;
  return <div className="test-result"><Badge status={job.data.status} /><JobList jobs={[job.data]} notify={notify} onChange={job.refresh} />{['QUEUED', 'RUNNING'].includes(job.data.status) && <button onClick={() => { void api(`/jobs/${id}/cancel`, 'POST').then(job.refresh).catch(error => notify(error.message, true)); }}>取消试跑</button>}{job.data.output_asset_ids.map(asset => <Media key={asset} id={asset} kind={kind} label="试跑输出" />)}</div>;
}
