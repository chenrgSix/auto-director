import { ChevronDown } from 'lucide-react';
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
export function availableRoles(capability: WorkflowCapability) {
  return ['prompt', 'negative', 'camera_motion', 'motion_strength', 'width', 'height', 'batch', 'seed',
    ...(capability === 'IMAGE_TO_IMAGE' ? ['reference_image', 'style_reference'] : []),
    ...(capability.endsWith('TO_VIDEO') ? ['start_frame', 'duration', 'fps', 'reference_video', 'style_reference'] : []),
    ...(capability === 'FIRST_LAST_TO_VIDEO' ? ['end_frame'] : [])];
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

type Props = {workflow: Workflow; capability: WorkflowCapability; bindings: Record<string, Binding>; outputs: Record<string, string>; onAdvanced: () => void};
export function BindingGuide({ workflow, capability, bindings, outputs, onAdvanced }: Props) {
  const media = workflow.media_type;
  const required = requiredRoles(capability);
  if (workflow.capabilities.supports_video_reference && media === 'video') required.push('reference_video');
  const missing = required.filter(role => !bindingReady(workflow, bindings, role)).map(roleLabel);
  if (!workflow.workflow[outputs[media]]) missing.push('生成结果');
  return <section className="panel binding-guide" aria-label="工作流绑定概览">
    <div className="panel-title">生成准备 <span className={missing.length ? 'pending-text' : 'success-text'}>{missing.length ? `${missing.length} 项待配置` : '必需用途已绑定'}</span></div>
    <p className="muted">{missing.length ? `还需在实际字段中指定：${missing.join('、')}。可以使用 AI 建议或手动配置。` : '生成所需用途已绑定到实际字段。所有值与用途都在下方同一张参数表中调整。'}</p>
    <button className="text-action" onClick={onAdvanced}><ChevronDown size={14} />查看实际参数与输出</button>
  </section>;
}

export function WorkflowReadiness({workflow, dirty, busy, onCheck, onAdvanced}: {workflow: Workflow; dirty: boolean; busy: boolean; onCheck: () => void; onAdvanced: () => void}) {
  const dependencies = workflow.validation?.issues.filter(i => ['MISSING_MODEL', 'MISSING_NODE'].includes(i.code)) || [];
  const other = workflow.validation?.issues.filter(i => !['MISSING_MODEL', 'MISSING_NODE'].includes(i.code)) || [];
  return <section className="panel workflow-readiness" aria-label="模型与节点检查">
    <div className="panel-title">模型与节点 <span className={workflow.validation?.valid && !dirty ? 'success-text' : 'pending-text'}>{dirty ? '修改后待检查' : workflow.validation?.valid ? '依赖检查通过' : dependencies.length ? '依赖待处理' : workflow.validation ? '参数待确认' : '尚未检查'}</span></div>
    <p className="muted">需要时单独检查 ComfyUI 的模型和节点。此检查可能较慢，不影响工作流导入。</p>
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
