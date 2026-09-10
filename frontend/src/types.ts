export type Value = string | number | boolean;
export type Problem = { code: string; message: string; details?: unknown };
export type Binding = { node_id: string; input: string; transform?: string; frame_multiple?: number; frame_offset?: number };
export type Capabilities = { supports_start_frame: boolean; supports_end_frame: boolean; supports_video_reference: boolean; supports_multi_reference: boolean; max_duration: number; low_memory_workflow_id: string | null };
export type Parameter = { key: string; node_id: string; field: string; class_type: string; type: string; default: Value; min: number | null; max: number | null; step: number | null; enum: Value[] | null; role: string | null };
export type Workflow = {
  id: string; name: string; type: 'image' | 'video'; workflow: Record<string, { class_type: string; inputs: Record<string, unknown> }>;
  capabilities: Capabilities; bindings: Record<string, Binding>; outputs: Record<string, string>;
  parameters: Parameter[]; parameter_values: Record<string, Value>; warnings: string[];
  validation: { valid: boolean; issues: Problem[] } | null; workflow_hash: string; last_test_job_id: string | null;
};
export type Settings = {
  duration_policy: { min: number; max: number; presets: number[] };
  comfyui_url: string; allow_public_comfyui: boolean; default_image: string; default_video: string;
  llm_base_url: string; llm_configured: boolean; llm_model: string; llm_api_key_configured: boolean;
  vlm_configured: boolean; vlm_model: string; max_asset_mb: number;
  render_timeout: number; request_timeout: number; poll_interval: number;
};
export type QA = { stage: string; character_consistency: number; scene_consistency: number; style_consistency: number; action_accuracy: number; transition_quality: number; artifact_score: number; explanation: string; retry_scope: string | null };
export type Shot = {
  id: string; title: string; index: number; duration: number; enabled: boolean; status: string; action: string; camera: string;
  transition_from_previous: string; start_frame_asset_id: string | null; end_frame_asset_id: string | null; video_asset_id: string | null;
  prompts: { start_frame_prompt: string; end_frame_prompt: string; video_prompt: string; negative_prompt: string } | null;
  error: Problem | null; qa: QA[];
};
export type Episode = {
  id: string; idea: string; title: string | null; target_duration: number; aspect_ratio: string; style: string; quality: string;
  status: string; shots: Shot[]; references: Record<string, string>; warnings: string[]; error: Problem | null;
  final_video_asset_id: string | null; final_duration?: number; created_at: string; metrics: Record<string, number>;
  bible: unknown; plan: unknown;
};
export type Asset = { id: string; type: string; episode_id: string; path: string; metadata: { kind: string }; size: number };
export type Job = { id: string; type: string; status: string; comfy_prompt_id: string | null; error: Problem | null; output_asset_ids: string[]; progress: { value?: number; max?: number; node?: string } | null; input_values: Record<string, unknown> };
export const ACTIVE = new Set(['QUEUED', 'PLANNING', 'BUILDING_BIBLE', 'GENERATING_REFERENCES', 'GENERATING_KEYFRAMES', 'RENDERING_VIDEO', 'QA', 'COMPOSING']);
export const STATUS: Record<string, string> = {
  DRAFT: '草稿', QUEUED: '等待开始', PLANNING: '规划故事', BUILDING_BIBLE: '建立视觉设定', GENERATING_REFERENCES: '生成参考图',
  GENERATING_KEYFRAMES: '生成关键帧', RENDERING_VIDEO: '渲染视频', QA: '质量检查', COMPOSING: '合成短片', COMPLETED: '已完成', FAILED: '待处理', CANCELLED: '已取消',
  PENDING: '待生成', GENERATING_START_FRAME: '生成首帧', START_FRAME_READY: '首帧就绪', GENERATING_END_FRAME: '生成尾帧', KEYFRAMES_READY: '关键帧就绪',
  VIDEO_READY: '视频就绪', PASSED: '已通过', STALE: '需要重新生成', RUNNING: '执行中', UNKNOWN: '需要核对',
};
