import { Check, ChevronDown, CircleHelp } from 'lucide-react';
import type { Binding, Workflow, WorkflowCapability } from './types';

export const ROLE_LABELS: Record<string, string> = {
  prompt: '画面描述', negative: '避免出现的内容', start_frame: '起始画面', end_frame: '结束画面',
  reference_image: '参考图片', style_reference: '风格参考', reference_video: '参考视频',
  reference_audio: '参考音频', duration: '镜头时长', fps: '视频帧率', width: '画面宽度',
  height: '画面高度', batch: '生成数量', seed: '随机种子', camera_motion: '镜头运动', motion_strength: '运动幅度',
};
export function roleLabel(role: string) { return ROLE_LABELS[role] || (role.startsWith('reference_image_') ? `参考图片 ${role.split('_').at(-1)}` : role); }
export function requiredRoles(capability: WorkflowCapability) {
  return ['prompt', ...(capability === 'IMAGE_TO_IMAGE' ? ['reference_image'] : []),
    ...(capability.endsWith('TO_VIDEO') ? ['start_frame', 'duration'] : []),
    ...(capability === 'FIRST_LAST_TO_VIDEO' ? ['end_frame'] : [])];
}
export function visibleRoles(capability: WorkflowCapability, bindings: Record<string, Binding>) {
  const roles = ['prompt', 'negative', 'camera_motion', 'motion_strength', 'width', 'height', 'batch', 'seed',
    ...(capability.endsWith('TO_VIDEO') ? ['start_frame', 'duration', 'fps', 'reference_video'] : ['reference_image', 'style_reference']),
    ...(capability === 'FIRST_LAST_TO_VIDEO' ? ['end_frame'] : []), ...Object.keys(bindings)];
  return [...new Set(roles)].filter(role => capability !== 'IMAGE_TO_VIDEO' || role !== 'end_frame');
}
export function bindingKey(binding?: Binding) { return binding ? `${binding.node_id}.${binding.input}` : ''; }
export function bindingReady(workflow: Workflow, bindings: Record<string, Binding>, role: string) {
  const key = bindingKey(bindings[role]);
  return !!key && workflow.parameters.some(p => p.key === key)
    && Object.values(bindings).filter(b => bindingKey(b) === key).length === 1;
}
export function parameterLabel(workflow: Workflow, key: string) {
  const p = workflow.parameters.find(p => p.key === key);
  if (!p) return key;
  const title = workflow.workflow[p.node_id]?._meta?.title || p.class_type;
  return `${title} · ${p.field}（${p.node_id}）`;
}

type Props = {workflow: Workflow; capability: WorkflowCapability; bindings: Record<string, Binding>; outputs: Record<string, string>; onBinding: (role: string, key: string) => void; onOutput: (id: string) => void; onAdvanced: () => void};
export function BindingGuide({ workflow, capability, bindings, outputs, onBinding, onOutput, onAdvanced }: Props) {
  const media = workflow.media_type;
  const required = requiredRoles(capability);
  if (workflow.capabilities.supports_video_reference) required.push('reference_video');
  const roles = [...new Set([...required, ...['negative', 'style_reference', 'reference_video'].filter(r => bindings[r])])];
  const outputOptions = workflow.binding_assistance?.outputs[media] || [];
  const outputReady = !!workflow.workflow[outputs[media]];
  const pending = roles.filter(r => required.includes(r) && !bindingReady(workflow, bindings, r)).length + (outputReady ? 0 : 1);
  const ownerText = (role: string) => role === 'duration' ? '导演根据总时长自动计算' : ['prompt', 'negative'].includes(role) ? 'AI 自动生成' : '生成时自动匹配素材';
  return <section className="panel binding-guide" aria-label="工作流识别概览">
    <div className="panel-title">输入与输出 <span className={pending ? 'pending-text' : 'success-text'}>{pending ? `${pending} 项待确认` : '必需项目已识别'}</span></div>
    <p className="muted">已识别的项目会自动用于生成。只需处理下方待确认的项目。</p>
    <div className="binding-summary">{roles.map(role => {
      const ready = bindingReady(workflow, bindings, role);
      const candidates = workflow.binding_assistance?.inputs[role] || [];
      return <div className={`binding-summary-item ${ready ? '' : 'needs-choice'}`} key={role}>
        {ready ? <Check size={17} className="success-text" /> : <CircleHelp size={17} />}
        <div><strong>{roleLabel(role)}</strong><small>{ready ? ownerText(role) : candidates.length > 1 ? `发现 ${candidates.length} 个候选，请确认用途` : '需要确认工作流中的对应输入'}</small>
          {ready ? <details className="binding-detail"><summary>查看识别结果</summary><p>{parameterLabel(workflow, bindingKey(bindings[role]))}</p></details> : <>
            {!!candidates.length && <select aria-label={`确认${roleLabel(role)}`} value="" onChange={e => onBinding(role, e.target.value)}>
              <option value="">选择对应输入</option>{candidates.map(c => <option key={c.key} value={c.key} disabled={role === 'duration' && !c.automatic}>{c.label} · {c.field}（{c.node_id}）{!c.automatic && role === 'duration' ? ' · 请确认帧数规则' : ''}</option>)}
            </select>}
            <button className="text-action" onClick={onAdvanced}>在高级设置中确认{role === 'duration' ? '时长规则' : '绑定'}</button>
          </>}
        </div>
      </div>;
    })}<div className={`binding-summary-item ${outputReady ? '' : 'needs-choice'}`}>
      {outputReady ? <Check size={17} className="success-text" /> : <CircleHelp size={17} />}
      <div><strong>生成结果</strong><small>{outputReady ? (media === 'video' ? '保存生成的视频' : '保存生成的图片') : outputOptions.length > 1 ? `发现 ${outputOptions.length} 个保存节点，请选择最终输出` : '请选择保存生成结果的节点'}</small>
        {outputReady ? <details className="binding-detail"><summary>查看识别结果</summary><p>{workflow.workflow[outputs[media]]?._meta?.title || workflow.workflow[outputs[media]]?.class_type}（{outputs[media]}）</p></details> : <>
          {!!outputOptions.length && <select aria-label="确认生成结果" value="" onChange={e => onOutput(e.target.value)}><option value="">选择最终输出</option>{outputOptions.map(c => <option key={c.node_id} value={c.node_id}>{c.label}（{c.node_id}）</option>)}</select>}
          <button className="text-action" onClick={onAdvanced}>在高级设置中选择输出</button>
        </>}
      </div>
    </div></div>
    <button className="text-action" onClick={onAdvanced}><ChevronDown size={14} />调整已识别的项目</button>
  </section>;
}

export function WorkflowReadiness({workflow, dirty, busy, onCheck, onAdvanced}: {workflow: Workflow; dirty: boolean; busy: boolean; onCheck: () => void; onAdvanced: () => void}) {
  const dependencies = workflow.validation?.issues.filter(i => ['MISSING_MODEL', 'MISSING_NODE'].includes(i.code)) || [];
  const other = workflow.validation?.issues.filter(i => !['MISSING_MODEL', 'MISSING_NODE'].includes(i.code)) || [];
  return <section className="panel workflow-readiness" aria-label="模型与节点检查">
    <div className="panel-title">模型与节点 <span className={workflow.validation?.valid && !dirty ? 'success-text' : 'pending-text'}>{dirty ? '修改后待检查' : workflow.validation?.valid ? '依赖检查通过' : dependencies.length ? '依赖待处理' : workflow.validation ? '参数待确认' : '尚未检查'}</span></div>
    <p className="muted">输入输出识别完成后，还需要检查 ComfyUI 的模型和节点是否可用。</p>
    {dirty && <div className="notice">配置已修改，请保存并重新检查依赖。</div>}
    {!!dependencies.length && <div className="dependency-problems">{dependencies.map((issue, index) => {
      const details = issue.details as {value?: unknown} | undefined;
      const node = issue as typeof issue & {class_type?: string};
      return <div className="notice" key={index}><strong>{issue.code === 'MISSING_MODEL' ? '缺少模型' : '缺少节点'}</strong><p>{typeof details?.value === 'string' ? details.value : node.class_type || issue.message}</p><small>{issue.code === 'MISSING_MODEL' ? '请在 ComfyUI 准备对应模型，或导入匹配现有模型的工作流。' : '请在 ComfyUI 安装对应节点后重新检查。'}</small><details><summary>技术详情</summary><pre>{JSON.stringify(issue, null, 2)}</pre></details></div>;
    })}</div>}
    {!!other.length && <div className="notice"><strong>输入输出或参数需要确认</strong><p>请检查上方待确认项；需要时可在高级设置中调整。</p><details><summary>查看具体问题</summary>{other.map((issue, i) => <p key={i}>{issue.message}</p>)}</details></div>}
    {!dirty && workflow.validation?.valid && <p className="success-text">依赖检查通过，可以进行下方试跑。</p>}
    <div className="actions"><button disabled={busy} onClick={onCheck}>保存并检查依赖</button>{!!dependencies.length && <button onClick={onAdvanced}>查看模型参数</button>}</div>
  </section>;
}
