import type { Binding, Parameter, ParameterOwner, Workflow, WorkflowCapability } from './types';

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
    ...(capability === 'IMAGE_TO_IMAGE' ? ['reference_image', 'style_reference', 'reference_image_1', 'reference_image_2'] : []),
    ...(capability.endsWith('TO_VIDEO') ? ['start_frame', 'duration', 'fps', 'reference_video', 'style_reference'] : []),
    ...(capability === 'FIRST_LAST_TO_VIDEO' ? ['end_frame'] : [])];
}
export function isAssetRole(role: string) { return ['start_frame', 'end_frame', 'reference_image', 'style_reference', 'reference_video', 'reference_audio'].includes(role) || role.startsWith('reference_image_'); }
export function roleOwner(role: string): ParameterOwner {
  if (isAssetRole(role)) return 'asset_resolver';
  if (['prompt', 'negative', 'camera_motion', 'motion_strength'].includes(role)) return 'ai';
  if (role === 'duration') return 'director';
  return 'system';
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
  return `${workflow.workflow[p.node_id]?._meta?.title || p.class_type} · ${p.field}（${p.node_id}）`;
}
function acceptsRole(item: Parameter, role: string) {
  if (item.asset_kind) return isAssetRole(role) && (item.asset_kind === 'video' ? role === 'reference_video' : item.asset_kind === 'audio' ? role === 'reference_audio' : !['reference_video', 'reference_audio'].includes(role));
  const numeric = ['integer', 'number'].includes(item.type) || (!!item.enum?.length && item.enum.every(v => typeof v === 'number'));
  if (['duration', 'fps', 'width', 'height', 'batch', 'seed', 'motion_strength'].includes(role)) return numeric;
  return !numeric && item.type !== 'boolean';
}
const HELP: Record<string, string> = {
  prompt: '选择接收画面描述的文本字段，生成时由 AI 填写。',
  start_frame: '选择接收首帧图片的字段，生成时自动传入真实图片。',
  end_frame: '选择接收尾帧图片的字段，生成时自动传入真实图片。',
  duration: '选择控制视频长度的字段；单位是帧时，再开启帧数转换。',
  reference_image: '选择图生图的图片输入，生成时自动绑定参考素材。',
};

type Props = {workflow: Workflow; bindings: Record<string, Binding>; outputs: Record<string, string>; onBind: (role: string, binding?: Binding) => void; onOutput: (id: string) => void; extraRoles: string[]; onAddRole: (role: string) => void};
export function BindingEditor({ workflow, bindings, outputs, onBind, onOutput, extraRoles, onAddRole }: Props) {
  const required = requiredRoles(workflow.capability);
  if (workflow.capabilities.supports_video_reference && workflow.media_type === 'video') required.push('reference_video');
  const roles = [...new Set([...required, ...Object.keys(bindings), ...extraRoles])];
  const optional = availableRoles(workflow.capability).filter(role => !roles.includes(role) && workflow.parameters.some(p => acceptsRole(p, role)));
  return <section className="binding-editor" aria-label="输入与输出绑定"><h3>生成需要哪些输入</h3><p className="muted">为每个用途选择工作流里的实际字段。这里指定填到哪里，生成时的内容由系统自动填写。</p>
    {roles.map(role => {
      const key = bindingKey(bindings[role]);
      const choices = workflow.parameters.filter(p => p.key === key || acceptsRole(p, role));
      return <div className="role-config" key={role}><label htmlFor={`bind-${role}`}>{roleLabel(role)} <small className={bindingReady(workflow, bindings, role) ? 'success-text' : 'pending-text'}>{bindingReady(workflow, bindings, role) ? '已绑定' : required.includes(role) ? '必填 · 待选择' : '可选'}</small></label>
        <select id={`bind-${role}`} value={key} onChange={event => { const p = workflow.parameters.find(p => p.key === event.target.value); onBind(role, p ? {node_id: p.node_id, input: p.field, transform: 'identity', frame_multiple: 4, frame_offset: 1} : undefined); }}>
          <option value="">请选择实际字段</option>{key && !choices.some(p => p.key === key) && <option value={key}>原绑定已失效：{key}</option>}
          {choices.map(p => { const occupied = Object.entries(bindings).find(([r, b]) => r !== role && bindingKey(b) === p.key)?.[0]; return <option key={p.key} value={p.key} disabled={!!occupied}>{parameterLabel(workflow, p.key)} · {String(workflow.parameter_values[p.key] ?? p.default).replace(/\s+/g, ' ').slice(0, 65)}{occupied ? `（已用于${roleLabel(occupied)}）` : ''}</option>; })}
        </select><small className="muted">{HELP[role] || '只绑定此工作流实际使用的字段；可选用途可留空。'}</small>
        {!choices.length && <p className="notice">没有符合类型的可写字段。请在 ComfyUI 把对应输入暴露为值，再导出 API JSON。</p>}
        {role === 'duration' && bindings[role] && <div className="transform-fields"><label>时长单位<select aria-label="时长转换" value={bindings[role].transform || 'identity'} onChange={event => onBind(role, {...bindings[role], transform: event.target.value})}><option value="identity">秒 · 直接使用</option><option value="duration_to_frames">帧 · 按 FPS 转换</option></select></label>{bindings[role].transform === 'duration_to_frames' && <><label>帧数倍数<input aria-label="帧数倍数" type="number" min={1} max={32} step={1} required value={bindings[role].frame_multiple ?? 4} onChange={event => onBind(role, {...bindings[role], frame_multiple: Number(event.target.value)})} /></label><label>帧数偏移<select aria-label="帧数偏移" value={bindings[role].frame_offset ?? 1} onChange={event => onBind(role, {...bindings[role], frame_offset: Number(event.target.value)})}><option value={1}>+1 帧</option><option value={0}>+0 帧</option></select></label></>}</div>}
      </div>;
    })}
    {!!optional.length && <div className="field"><label htmlFor="add-binding">添加其他用途（可选）</label><select id="add-binding" value="" onChange={event => onAddRole(event.target.value)}><option value="">例如负面提示词、尺寸、随机种子…</option>{optional.map(role => <option key={role} value={role}>{roleLabel(role)}</option>)}</select></div>}
    <div className="role-config"><label htmlFor="output-node">生成结果 <small className={workflow.workflow[outputs[workflow.media_type]] ? 'success-text' : 'pending-text'}>{workflow.workflow[outputs[workflow.media_type]] ? '已绑定' : '必填 · 待选择'}</small></label><select id="output-node" value={outputs[workflow.media_type] || ''} onChange={event => onOutput(event.target.value)}><option value="">请选择保存结果的节点</option>{Object.entries(workflow.workflow).map(([id, node]) => <option key={id} value={id}>{node._meta?.title || node.class_type}（{id}） · {node.class_type}</option>)}</select><small className="muted">选择最终保存{workflow.media_type === 'image' ? '图片' : '视频'}的节点，例如 SaveImage / SaveVideo。</small></div>
  </section>;
}

export function WorkflowReadiness({workflow, dirty, busy, onCheck, onLocate}: {workflow: Workflow; dirty: boolean; busy: boolean; onCheck: () => void; onLocate: (key?: string) => void}) {
  return <section className="workflow-readiness" aria-label="模型与节点检查"><h3>检查模型、节点与参数</h3><p className="muted">从 ComfyUI 获取当前可用模型和参数范围，不会开始生成。{dirty ? '检查前会保存修改。' : ''}</p>
    <button type="button" disabled={busy} onClick={onCheck}>保存并检查依赖</button>
    {!dirty && workflow.validation?.valid && <p className="success-text">依赖检查通过，可以试跑。</p>}
    {workflow.validation?.issues.map((raw, index) => {
      const issue = raw as typeof raw & {node_id?: string; field?: string; parameter?: string; role?: string; class_type?: string};
      const key = issue.parameter || (issue.node_id && issue.field ? `${issue.node_id}.${issue.field}` : undefined);
      const writable = key && workflow.parameters.some(p => p.key === key);
      const details = issue.details as {value?: unknown} | undefined;
      return <div className="notice config-problem" key={index}><strong>{issue.code === 'MISSING_MODEL' ? '模型不可用' : issue.code === 'MISSING_NODE' ? '缺少节点' : '配置需要调整'}</strong><p>{issue.message}{issue.role ? `：${roleLabel(issue.role)}` : ''}</p>{key && <p>{parameterLabel(workflow, key)}</p>}{details?.value != null && <p>{String(details.value)}</p>}
        {writable || issue.role || (!issue.node_id && !key) ? <button type="button" onClick={() => onLocate(writable ? key : undefined)}>{writable ? '去修改此参数' : '去修改输入与输出'}</button> : <small>请在 ComfyUI {issue.code === 'MISSING_NODE' ? `安装 ${issue.class_type || '对应节点'}` : '修正此节点的输入或连线，并重新导出工作流'}后检查。</small>}
        <details><summary>技术详情</summary><pre>{JSON.stringify(issue, null, 2)}</pre></details></div>;
    })}
  </section>;
}
