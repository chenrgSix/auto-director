import { useEffect, useRef, useState } from 'react';
import { Check, Play, Save, Star, Trash2 } from 'lucide-react';
import { ApiError, api, useResource } from './api';
import type { Notify } from './App';
import { JobList } from './EpisodePage';
import type { Asset, Binding, Capabilities, Job, ParameterRule, Settings, Value, Workflow, WorkflowCapability } from './types';
import { CAPABILITY_LABELS } from './types';
import { Badge, ErrorNotice, Loading, Media } from './ui';
import { BindingEditor, WorkflowReadiness, bindingKey, bindingReady, isAssetRole, requiredRoles, roleLabel } from './WorkflowBindings';
import { WorkflowAI, mergeBindings } from './WorkflowAI';
import { WorkflowParameterPanel } from './WorkflowParameterPanel';

type Pane = 'bindings' | 'parameters' | 'checks';
const PANES: [Pane, string][] = [['bindings', '输入与输出'], ['parameters', '模型与参数'], ['checks', '检查与试跑']];
export function WorkflowEditor({ workflow, isDefault, notify, refresh, maxShotSeconds, onDirtyChange }: { workflow: Workflow; maxShotSeconds?: number; isDefault: boolean; notify: Notify; refresh: () => void; onDirtyChange: (dirty: boolean) => void }) {
  const [saved, setSaved] = useState(workflow);
  const [name, setName] = useState(workflow.name);
  const [bindings, setBindings] = useState<Record<string, Binding>>(workflow.bindings);
  const [outputs, setOutputs] = useState(workflow.outputs);
  const [capability, setCapability] = useState<WorkflowCapability>(workflow.capability);
  const [rules, setRules] = useState<Record<string, ParameterRule>>(workflow.parameter_rules);
  const [caps, setCaps] = useState<Capabilities>(workflow.capabilities);
  const [parameters, setParameters] = useState<Record<string, Value>>(workflow.parameter_values);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pane, setPane] = useState<Pane>('bindings');
  const [search, setSearch] = useState('');
  const [extraRoles, setExtraRoles] = useState<string[]>([]);
  const [testPrompt, setTestPrompt] = useState('A cinematic landscape, natural light.');
  const [testDuration, setTestDuration] = useState(Math.min(2, caps.max_duration));
  const [testAssets, setTestAssets] = useState<Record<string, string>>({});
  const [jobId, setJobId] = useState(workflow.last_test_job_id);
  const form = useRef<HTMLFormElement>(null);
  const changes = {name, bindings, outputs, capability, capabilities: caps, parameter_values: parameters, parameter_rules: rules};
  const dirty = Object.entries(changes).some(([key, value]) => JSON.stringify(value) !== JSON.stringify(saved[key as keyof Workflow]));
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);
  const draft = {...saved, capabilities: caps, capability, parameter_values: parameters};
  const testFps = bindings.duration?.frame_fps ?? 16;
  const required = requiredRoles(capability);
  if (caps.supports_video_reference && workflow.media_type === 'video') required.push('reference_video');
  const missing = required.filter(role => !bindingReady(draft, bindings, role)).map(roleLabel);
  if (!workflow.workflow[outputs[workflow.media_type]]) missing.push('生成结果');
  const unboundAssets = saved.parameters.filter(p => p.asset_kind && !Object.entries(bindings).some(([role, b]) => isAssetRole(role) && bindingKey(b) === p.key));
  function acceptWorkflow(result: Workflow) {
    setSaved(result); setName(result.name); setBindings(result.bindings); setOutputs(result.outputs);
    setCapability(result.capability); setCaps(result.capabilities); setRules(result.parameter_rules); setParameters(result.parameter_values); setExtraRoles([]);
  }
  function bind(role: string, binding?: Binding) {
    const next = {...bindings};
    if (binding) next[role] = binding; else delete next[role];
    setBindings(next);
    const updated = {...rules};
    for (const key of [bindingKey(bindings[role]), bindingKey(binding)]) {
      if (updated[key]?.owner) updated[key] = {editable: updated[key].editable, override_policy: updated[key].override_policy};
    }
    setRules(updated);
  }
  function validInputs(includeTest = false) {
    const invalid = Array.from(form.current?.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>('input, select, textarea') || []).find(input => (includeTest || !input.closest('[data-test-inputs]')) && !input.checkValidity());
    if (!invalid) return true;
    const targetPane = invalid.closest<HTMLElement>('[data-pane]')?.dataset.pane as Pane | undefined;
    if (targetPane) setPane(targetPane);
    setError('请修正标出的字段，数值需符合工作流允许的范围。');
    requestAnimationFrame(() => { invalid.scrollIntoView({block: 'center'}); invalid.reportValidity(); });
    return false;
  }
  function failure(reason: unknown) {
    const message = (reason as Error).message; setError(message); notify(message, true);
    if (reason instanceof ApiError) {
      const details = reason.problem.details as {key?: string; parameter?: {key?: string}} | undefined;
      const key = details?.key || details?.parameter?.key;
      if (key && saved.parameters.some(p => p.key === key)) locate(key);
    }
  }
  async function persist() {
    const result = await api<Workflow>(`/workflows/${workflow.id}`, 'PATCH', changes);
    acceptWorkflow(result); refresh(); return result;
  }
  async function save(check = false) {
    if (!validInputs()) return;
    setBusy(true); setError('');
    try {
      await persist();
      if (check) { const checked = await api<Workflow>(`/workflows/${workflow.id}/validate`, 'POST'); acceptWorkflow(checked); refresh(); setPane('checks'); }
      notify(check ? '检查已完成，结果显示在下方' : '配置已保存');
    } catch (reason) { failure(reason); }
    finally { setBusy(false); }
  }
  async function setDefault() {
    if (!validInputs()) return;
    setBusy(true); setError('');
    try { if (dirty) await persist(); await api<Settings>(`/workflows/${workflow.id}/default`, 'POST'); refresh(); notify('已设为默认工作流，新建短片时生效'); }
    catch (reason) { failure(reason); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!window.confirm(`删除工作流「${saved.name}」？`)) return;
    setBusy(true); setError('');
    try { await api(`/workflows/${workflow.id}`, 'DELETE'); onDirtyChange(false); refresh(); notify('工作流已删除'); }
    catch (reason) { failure(reason); }
    finally { setBusy(false); }
  }
  async function upload(role: string, file?: File) {
    if (!file) return;
    setBusy(true); setError('');
    const data = new FormData(); data.append('file', file);
    try { const asset = await api<Asset>('/assets', 'POST', data); setTestAssets(current => ({...current, [role]: asset.id})); notify(`${roleLabel(role)}已上传`); }
    catch (reason) { failure(reason); }
    finally { setBusy(false); }
  }
  async function testRun() {
    if (!validInputs(true)) return;
    setBusy(true); setError('');
    try {
      await persist();
      const checked = await api<Workflow>(`/workflows/${workflow.id}/validate`, 'POST'); acceptWorkflow(checked); refresh();
      if (!checked.validation?.valid) { setError('请先处理上方检查结果，再试跑。'); return; }
      const job = await api<Job>(`/workflows/${workflow.id}/test-run`, 'POST', {values: {prompt: testPrompt, duration: testDuration, fps: testFps}, asset_bindings: testAssets});
      setJobId(job.id); refresh(); notify('试跑已进入渲染队列');
    } catch (reason) { failure(reason); }
    finally { setBusy(false); }
  }
  function locate(key?: string) { setSearch(key || ''); setPane(key ? 'parameters' : 'bindings'); requestAnimationFrame(() => form.current?.querySelector<HTMLElement>(key ? '#parameter-search' : '.binding-editor')?.scrollIntoView({block: 'center', behavior: 'smooth'})); }
  return <form className="workflow-editor" ref={form} onSubmit={event => event.preventDefault()}><fieldset disabled={busy} className="workflow-fieldset">
    <section className="panel"><div className="editor-heading"><div><span className="small-label">WORKFLOW PROFILE</span><h2>{saved.name}</h2></div><div className="actions"><button type="button" disabled={isDefault || !!missing.length || !!unboundAssets.length} onClick={() => void setDefault()}><Star size={14} />{isDefault ? '默认工作流' : dirty ? '保存并设为默认' : '设为默认'}</button><button type="button" className="icon-button" aria-label="删除当前工作流" disabled={isDefault} onClick={() => void remove()}><Trash2 size={16} /></button></div></div>
      <div className="fields two"><div className="field"><label htmlFor="workflow-name">名称</label><input id="workflow-name" required value={name} maxLength={120} onChange={event => setName(event.target.value)} /></div><div className="field"><label htmlFor="workflow-capability">生成用途</label><select id="workflow-capability" value={capability} onChange={event => { const value = event.target.value as WorkflowCapability; setCapability(value); setExtraRoles([]); setCaps({...caps, supports_end_frame: value === 'FIRST_LAST_TO_VIDEO'}); if (value === 'IMAGE_TO_VIDEO') bind('end_frame'); }}>{Object.entries(CAPABILITY_LABELS).filter(([key]) => (key.endsWith('TO_IMAGE') ? 'image' : 'video') === workflow.media_type).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div></div>
      <p className={missing.length || unboundAssets.length ? 'pending-text' : 'success-text'}>{missing.length ? `还需绑定：${missing.join('、')}。` : unboundAssets.length ? `还有 ${unboundAssets.length} 个素材字段未指定用途。` : '输入与输出已绑定完整，可以设为默认。'}</p>
    </section>
    <div className="workflow-toolbar"><div className="workflow-tabs" role="tablist" aria-label="工作流配置">{PANES.map(([id, label]) => <button type="button" role="tab" key={id} id={`tab-${id}`} aria-selected={pane === id} aria-controls={`pane-${id}`} onClick={() => setPane(id)}>{label}</button>)}</div><div className="workflow-save"><span role="status">{busy ? '正在处理…' : dirty ? '有未保存修改' : '配置已保存'}</span>{dirty && <button type="button" onClick={() => { acceptWorkflow(saved); setError(''); }}>取消修改</button>}<button type="button" className="primary" onClick={() => void save()}><Save size={15} />保存配置</button></div>{error && <div className="notice" role="alert">{error}</div>}</div>
    <section className="panel workflow-pane" role="tabpanel" id="pane-bindings" aria-labelledby="tab-bindings" data-pane="bindings" hidden={pane !== 'bindings'}>
      <BindingEditor workflow={draft} bindings={bindings} outputs={outputs} extraRoles={extraRoles} onAddRole={role => setExtraRoles([...extraRoles, role])} onBind={bind} onOutput={id => setOutputs({...outputs, [workflow.media_type]: id})} />
      {!!unboundAssets.length && <div className="notice">尚未指定用途的素材字段：{unboundAssets.map(p => p.key).join('、')}。请在上方选择这些字段，或添加其他素材用途。</div>}
      <details className="config-details"><summary>让 AI 帮忙识别绑定（可选）</summary><WorkflowAI key={`${workflow.id}:${capability}`} graph={workflow.workflow} capability={capability} disabled={busy} onApply={proposal => { const next = mergeBindings(bindings, proposal.bindings); setBindings(next); const updated = {...rules}; for (const [role, b] of Object.entries(next)) { const key = bindingKey(b); if (!bindings[role] && updated[key]?.owner) updated[key] = {editable: updated[key].editable, override_policy: updated[key].override_policy}; } setRules(updated); setOutputs(current => ({...proposal.outputs, ...Object.fromEntries(Object.entries(current).filter(([, value]) => value))})); notify('AI 建议已填入空缺项，请确认后保存。'); }} /></details>
      {workflow.media_type === 'video' && <div className="capabilities"><h3>视频生成限制</h3><div className="field"><label htmlFor="max-duration">每镜最大时长（秒）</label><input id="max-duration" type="number" required min={1} max={maxShotSeconds} step={0.1} value={caps.max_duration} onChange={event => setCaps({...caps, max_duration: Number(event.target.value)})} /><small className="muted">导演按此上限与模型支持的时长分配分镜；低显存不会把已配置的时长强制缩短，实际显存不足时再降级处理。</small></div><details className="config-details"><summary>连续性与低显存设置</summary><div className="field"><label htmlFor="low-profile">低显存工作流 ID（可选）</label><input id="low-profile" value={caps.low_memory_workflow_id || ''} onChange={event => setCaps({...caps, low_memory_workflow_id: event.target.value || null})} /></div>{bindings.reference_video && <label className="checkbox"><input type="checkbox" checked={caps.supports_video_reference} onChange={event => setCaps({...caps, supports_video_reference: event.target.checked})} />需要上一镜视频参考</label>}</details></div>}
      {capability === 'IMAGE_TO_IMAGE' && Object.keys(bindings).some(role => role.startsWith('reference_image_')) && <label className="checkbox"><input type="checkbox" checked={caps.supports_multi_reference} onChange={event => setCaps({...caps, supports_multi_reference: event.target.checked})} />支持多参考图</label>}
    </section>
    <section className="panel workflow-pane" role="tabpanel" id="pane-parameters" aria-labelledby="tab-parameters" data-pane="parameters" hidden={pane !== 'parameters'}><WorkflowParameterPanel workflow={saved} bindings={bindings} values={parameters} rules={rules} search={search} onSearch={setSearch} onValues={setParameters} onRules={setRules} onBinding={() => locate()} /></section>
    <section className="panel workflow-pane" role="tabpanel" id="pane-checks" aria-labelledby="tab-checks" data-pane="checks" hidden={pane !== 'checks'}>
      <div className="execution-info"><h3>{saved.execution_info?.mode === 'cloud' ? '包含云端 API 节点' : saved.execution_info?.mode === 'local' ? '已检查：未发现 ComfyUI 云端 API 节点' : '运行方式尚未确认'}</h3><p>{saved.execution_info?.mode === 'cloud' ? 'ComfyUI 虽然部署在本地，这些节点仍调用云端服务，可能要求登录或 API 密钥。若要本地生成，请选用本地模型节点的工作流。' : saved.execution_info?.mode === 'local' ? '当前节点元数据未标记云端 API。模型是否安装、节点是否可用，请查看下面的依赖检查。第三方节点的外部服务需按其自身说明确认。' : '点击下方检查，从 ComfyUI 的节点元数据确认；不会根据工作流或模型名称猜测。'}</p>{saved.execution_info?.api_nodes.map(node => <small key={node.id}>{node.title} · {node.class_type}（{node.id}）</small>)}</div>
      <WorkflowReadiness workflow={saved} dirty={dirty} busy={busy} onCheck={() => void save(true)} onLocate={locate} />
      <div className="test-panel capabilities" data-test-inputs><h3>试跑验证</h3><p className="muted">以下操作会提交真实生成任务。先检查配置，再提供测试描述与素材。</p><div className="field"><label htmlFor="test-prompt">测试画面描述</label><textarea id="test-prompt" rows={3} value={testPrompt} onChange={event => setTestPrompt(event.target.value)} /></div>{workflow.media_type === 'video' && <div className="field"><label htmlFor="test-duration">试跑时长（秒，{testFps} FPS）</label><input id="test-duration" type="number" required min={1} max={caps.max_duration} step={0.1} value={testDuration} onChange={event => setTestDuration(Number(event.target.value))} /></div>}{Object.keys(bindings).filter(isAssetRole).map(role => <label key={role} className="file-field"><span>{roleLabel(role)} {testAssets[role] && <Check size={14} />}</span><input type="file" accept={role === 'reference_video' ? 'video/*' : role === 'reference_audio' ? 'audio/*' : 'image/png,image/jpeg,image/webp'} onChange={event => void upload(role, event.target.files?.[0])} /></label>)}<button type="button" disabled={!testPrompt.trim() || !!missing.length || !!unboundAssets.length} onClick={() => void testRun()}><Play size={15} />保存并试跑</button>{jobId && <TestJob id={jobId} kind={workflow.media_type} notify={notify} />}</div>
    </section>
  </fieldset></form>;
}
function TestJob({ id, kind, notify }: { id: string; kind: 'image' | 'video'; notify: Notify }) {
  const job = useResource<Job>(`/jobs/${id}`, 1500);
  if (!job.data) return job.error ? <ErrorNotice>{job.error}</ErrorNotice> : <Loading />;
  return <div className="test-result"><Badge status={job.data.status} /><JobList jobs={[job.data]} notify={notify} onChange={job.refresh} />{['QUEUED', 'RUNNING'].includes(job.data.status) && <button type="button" onClick={() => { void api(`/jobs/${id}/cancel`, 'POST').then(job.refresh).catch(error => notify(error.message, true)); }}>取消试跑</button>}{job.data.output_asset_ids.map(asset => <Media key={asset} id={asset} kind={kind} label="试跑输出" />)}</div>;
}
