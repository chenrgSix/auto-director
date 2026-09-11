export type Value = string | number | boolean;
export type Problem = { code: string; message: string; details?: unknown };
export type Binding = { node_id: string; input: string; transform?: string; frame_multiple?: number; frame_offset?: number };
export type Capabilities = { supports_start_frame: boolean; supports_end_frame: boolean; supports_video_reference: boolean; supports_multi_reference: boolean; max_duration: number; low_memory_workflow_id: string | null };
export type WorkflowCapability = 'TEXT_TO_IMAGE' | 'IMAGE_TO_IMAGE' | 'FIRST_LAST_TO_VIDEO' | 'IMAGE_TO_VIDEO';
export const CAPABILITY_LABELS: Record<WorkflowCapability, string> = { TEXT_TO_IMAGE: '文生图', IMAGE_TO_IMAGE: '图生图', FIRST_LAST_TO_VIDEO: '首尾帧生成视频', IMAGE_TO_VIDEO: '首帧生成视频' };
export type ParameterOwner = 'ai' | 'director' | 'asset_resolver' | 'system' | 'workflow' | 'user';
export const OWNER_LABELS: Record<ParameterOwner, string> = {ai:'AI 自动生成',director:'导演规划',asset_resolver:'自动绑定素材',system:'系统策略',workflow:'工作流默认',user:'用户填写'};
export type ParameterRule = {owner?: ParameterOwner; editable: boolean; override_policy: 'advanced' | 'never'};
export type Parameter = { owner: ParameterOwner; editable: boolean; override_policy: 'advanced' | 'never'; key: string; node_id: string; field: string; class_type: string; type: string; default: Value; min: number | null; max: number | null; step: number | null; enum: Value[] | null; role: string | null; asset_kind?: string | null };
export type BindingCandidate = {key: string; node_id: string; label: string; field: string; reason: string; automatic: boolean; binding: Binding};
export type BindingAssistance = {suggested_capability: WorkflowCapability | null; inputs: Record<string, BindingCandidate[]>; outputs: Record<string, {node_id: string; label: string; reason: string}[]>};
export type Workflow = {
  id: string; name: string; type: 'image' | 'video'; media_type: 'image' | 'video'; capability: WorkflowCapability; parameter_rules: Record<string, ParameterRule>; workflow: Record<string, { class_type: string; inputs: Record<string, unknown>; _meta?: {title?: string} }>;
  capabilities: Capabilities; bindings: Record<string, Binding>; outputs: Record<string, string>;
  parameters: Parameter[]; parameter_values: Record<string, Value>; warnings: string[];
  validation: { valid: boolean; issues: Problem[] } | null; workflow_hash: string; last_test_job_id: string | null;
  binding_assistance?: BindingAssistance; binding_issues?: Problem[];
  execution_info?: { mode: 'local' | 'cloud' | 'unknown'; api_nodes: {id: string; class_type: string; title: string}[] };
};
export type Settings = {
  default_capabilities: Partial<Record<WorkflowCapability, string>>;
  limits: { fps: {min:number;max:number}; episode_shots: number; shot_seconds: {min:number;max:number} };
  duration_policy: { min: number; max: number; presets: number[] };
  comfyui_url: string; allow_public_comfyui: boolean; default_image: string; default_video: string;
  llm_base_url: string; llm_configured: boolean; llm_model: string; llm_api_key_configured: boolean;
  vlm_configured: boolean; vlm_model: string; max_asset_mb: number;
  render_timeout: number; request_timeout: number; llm_timeout: number; poll_interval: number;
};
export type QA = { stage: string; character_consistency: number; scene_consistency: number; style_consistency: number; action_accuracy: number; transition_quality: number; artifact_score: number; explanation: string; retry_scope: string | null };
export type Shot = {
  id: string; title: string; index: number; duration: number; enabled: boolean; status: string; action: string; camera: string;
  transition_from_previous: string; start_frame_asset_id: string | null; end_frame_asset_id: string | null; video_asset_id: string | null;
  prompts: { start_frame_prompt: string; end_frame_prompt: string; video_prompt: string; negative_prompt: string; narration_text?: string; camera_motion?: string; motion_strength?: number; ai_parameters?: Record<string, Record<string, Value>> } | null;
  preview_prompt_view?: { values: Record<PreviewPromptField, string>; locked: Partial<Record<PreviewPromptField, string>>; hints?: Partial<Record<PreviewPromptField, string>> };
  error: Problem | null; qa: QA[];
};
export type PreviewPromptField = 'start_frame_prompt' | 'end_frame_prompt' | 'video_prompt';
export type PreviewShotEdit = Pick<Shot, 'id' | 'title' | 'duration'> & Record<PreviewPromptField, string>;
export type Episode = {
  version: number; image_workflow_id: string; video_workflow_id: string; reference_workflow_id: string;
  workflow_binding_history?: unknown[];
  recoverable_workflow_revision?: number;
  refresh_workflow_budget?: boolean;
  id: string; idea: string; title: string | null; target_duration: number; aspect_ratio: string; style: string; quality: string;
  status: string; shots: Shot[]; references: Record<string, string>; warnings: string[]; error: Problem | null;
  final_video_asset_id: string | null; final_duration?: number; created_at: string; metrics: Record<string, number>;
  bible: unknown; plan: unknown;
  preview_required?: boolean; preview_approved_at?: string | null;
  preview?: { capability: WorkflowCapability; min_duration: number; max_duration: number; render_max_duration: number; fixed_duration: number | null } | null;
};
export type Asset = { id: string; type: string; episode_id: string; path: string; metadata: { kind: string }; size: number };
export type Job = { id: string; type: string; status: string; comfy_prompt_id: string | null; error: Problem | null; output_asset_ids: string[]; progress: { value?: number; max?: number; node?: string; connection?: 'connected' | 'reconnecting'; reconnect_attempt?: number; retry_in?: number; message?: string } | null; input_values: Record<string, unknown> };
export const ACTIVE = new Set(['QUEUED', 'PLANNING', 'BUILDING_BIBLE', 'PREPARING_PROMPTS', 'GENERATING_REFERENCES', 'GENERATING_KEYFRAMES', 'RENDERING_VIDEO', 'QA', 'COMPOSING']);
export const STATUS: Record<string, string> = {
  DRAFT: '草稿', QUEUED: '等待开始', PLANNING: '规划故事', BUILDING_BIBLE: '建立视觉设定', GENERATING_REFERENCES: '生成参考图',
  PREPARING_PROMPTS: '编写镜头提示词', AWAITING_REVIEW: '待确认分镜',
  GENERATING_KEYFRAMES: '生成关键帧', RENDERING_VIDEO: '渲染视频', QA: '质量检查', COMPOSING: '合成短片', COMPLETED: '已完成', FAILED: '待处理', CANCELLED: '已取消',
  PENDING: '待生成', GENERATING_START_FRAME: '生成首帧', START_FRAME_READY: '首帧就绪', GENERATING_END_FRAME: '生成尾帧', KEYFRAMES_READY: '关键帧就绪',
  VIDEO_READY: '视频就绪', PASSED: '已通过', STALE: '需要重新生成', RUNNING: '执行中', UNKNOWN: '需要核对',
};
