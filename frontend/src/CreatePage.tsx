import { useState, type FormEvent } from 'react';
import { ArrowRight, ChevronDown, Clapperboard, Sparkles, Wand2 } from 'lucide-react';
import { api, useResource } from './api';
import { navigate, type Notify } from './App';
import type { Episode, QAPolicy, Settings, Value, Workflow } from './types';
import { QA_POLICIES } from './EpisodeQAPolicy';
import { ParameterOverrides } from './DynamicParameters';
import { ErrorNotice } from './ui';

const examples = ['三只狮子穿越到侏罗纪，在陌生的森林中遇见巨大的食草恐龙。', '一名宇航员在荒芜星球上，发现了一朵正在开放的花。', '清晨的城市还未苏醒，一只狐狸悄悄穿过空荡的街道。'];

export default function CreatePage({ notify }: { notify: Notify }) {
  const [idea, setIdea] = useState('');
  const [preset, setPreset] = useState<number | null>(10);
  const [customDuration, setCustomDuration] = useState('');
  const [ratio, setRatio] = useState('9:16');
  const [style, setStyle] = useState('自然纪录片');
  const [quality, setQuality] = useState('standard');
  const [qaPolicy, setQAPolicy] = useState<QAPolicy>('advisory');
  const [advancedMode, setAdvancedMode] = useState(false);
  const [overrides, setOverrides] = useState<Record<string, Record<string, Value>>>({});
  const [busy, setBusy] = useState(false);
  const workflows = useResource<Workflow[]>('/workflows');
  const config = useResource<Settings>('/settings');
  const durationPolicy = config.data?.duration_policy;
  const duration = preset ?? (customDuration.trim() ? Number(customDuration) : NaN);
  const validDuration = !!durationPolicy && Number.isFinite(duration) && duration >= durationPolicy.min && duration <= durationPolicy.max;
  const [advanced, setAdvanced] = useState({ image_workflow_id: '', video_workflow_id: '', reference_workflow_id: '', memory_mode: 'auto', fps: 16, seed: 42, max_retries: 2, qa_enabled: true });
  const selectedIds = [advanced.image_workflow_id || config.data?.default_image, advanced.video_workflow_id || config.data?.default_video, advanced.reference_workflow_id || config.data?.default_capabilities.TEXT_TO_IMAGE];
  const selectedWorkflows = workflows.data?.filter(item => selectedIds.includes(item.id)) ?? [];
  const activeOverrides = Object.fromEntries(selectedWorkflows.map(item => [item.id, overrides[item.id] ?? {}]));
  const missingAsset = advancedMode && selectedWorkflows.some(item => item.parameters.some(parameter => parameter.owner === 'asset_resolver' && Object.hasOwn(activeOverrides[item.id], parameter.key) && !activeOverrides[item.id][parameter.key]));
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!idea.trim() || busy || !validDuration || missingAsset) return;
    setBusy(true);
    try {
      const episode = await api<Episode>('/episodes', 'POST', { idea, target_duration: duration, aspect_ratio: ratio, style, quality, qa_policy: qaPolicy, preview_required: true, advanced_mode: advancedMode, ...(advancedMode ? { ...advanced, image_workflow_id: advanced.image_workflow_id || null, video_workflow_id: advanced.video_workflow_id || null, reference_workflow_id: advanced.reference_workflow_id || null, workflow_overrides: activeOverrides } : {}) });
      navigate(`episode/${episode.id}`);
      await api(`/episodes/${episode.id}/preview`, 'POST');
      notify('正在规划分镜，完成后请预览并确认');
    } catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  return <>
    <div className="page-heading"><div className="eyebrow"><span />你的下一部短片，从这里开始</div><h1>一个想法。<br /><span>一部属于你的短片。</span></h1><p>描述脑海中的画面，让导演把故事、镜头与视觉串在一起。</p></div>
    {workflows.error && <ErrorNotice>{workflows.error}</ErrorNotice>}
    {config.error && <ErrorNotice>{config.error}</ErrorNotice>}
    <div className="create-grid">
      <form className="create-form" onSubmit={submit}>
        <div className="panel idea-panel"><label htmlFor="idea" className="panel-title"><Wand2 size={18} />你想讲一个怎样的故事？</label><textarea id="idea" className="idea-input" value={idea} onChange={event => setIdea(event.target.value)} maxLength={2000} required placeholder="比如，三只狮子突然来到侏罗纪……" /><div className="idea-footer"><span>只需一句话，也可以写下更具体的画面。</span><span>{idea.length} / 2000</span></div></div>
        <div className="panel parameters-panel"><div className="panel-title">定义你的画面 <span className="muted">01 / CREATIVE DIRECTION</span></div>
          <div className="field"><label id="duration-label">短片时长</label><div className="duration-options" role="group" aria-labelledby="duration-label">{durationPolicy?.presets.map(value => <button key={value} type="button" aria-pressed={preset === value} className={preset === value ? 'selected' : ''} onClick={() => { setPreset(value); setCustomDuration(''); }}>{value}<span>秒</span></button>)}</div>
            <div className={`custom-duration ${preset === null ? 'selected' : ''}`}><label htmlFor="custom-duration">自定义</label><input id="custom-duration" type="number" inputMode="decimal" min={durationPolicy?.min} max={durationPolicy?.max} step="0.01" required={preset === null} disabled={!durationPolicy} value={customDuration} aria-describedby="duration-hint" aria-invalid={preset === null && !!customDuration && !validDuration} onFocus={() => setPreset(null)} onChange={event => { setPreset(null); setCustomDuration(event.target.value); }} placeholder={durationPolicy ? `${durationPolicy.min}～${durationPolicy.max}` : '加载中'} /><span>秒</span></div>
            <small id="duration-hint">{durationPolicy && `支持 ${durationPolicy.min}～${durationPolicy.max} 秒。`}导演将根据总时长和工作流能力自动规划镜头。</small>
            {preset === null && customDuration && !validDuration && durationPolicy && <small className="error-text" role="alert">请输入 {durationPolicy.min}～{durationPolicy.max} 秒的时长。</small>}
          </div>
          <div className="fields two"><div className="field"><label htmlFor="ratio">画幅比例</label><select id="ratio" value={ratio} onChange={event => setRatio(event.target.value)}><option value="9:16">9:16 · 竖屏</option><option value="16:9">16:9 · 宽银幕</option><option value="1:1">1:1 · 方形</option></select></div><div className="field"><label htmlFor="style">视觉风格</label><select id="style" value={style} onChange={event => setStyle(event.target.value)}>{['自然纪录片', '写实电影', '动画', '科幻', '手绘', '自动'].map(item => <option key={item}>{item}</option>)}</select></div></div>
          <div className="field"><label>质量模式</label><div className="quality-options">{[{ id: 'fast', title: '快速探索', text: '低分辨率 · 快速验证' }, { id: 'standard', title: '标准创作', text: '质量与生成成本平衡' }, { id: 'high', title: '精细打磨', text: qaPolicy === 'strict' ? '视觉候选 · 更高画面标准' : '更高画面标准 · 成片后复核' }].map(item => <button type="button" key={item.id} className={quality === item.id ? 'selected' : ''} aria-pressed={quality === item.id} onClick={() => { setQuality(item.id); setAdvanced({ ...advanced, max_retries: item.id === 'fast' ? 1 : item.id === 'high' ? 3 : 2 }); }}><span>{item.title}{item.id === 'standard' && <i>推荐</i>}</span><small>{item.text}</small></button>)}</div></div>
          <div className="field"><label htmlFor="qa-policy">质检策略</label><select id="qa-policy" value={qaPolicy} onChange={event => setQAPolicy(event.target.value as QAPolicy)}>{QA_POLICIES.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select><small>{QA_POLICIES.find(item => item.id === qaPolicy)?.detail}连接中断、生成失败等技术问题仍会暂停并保留进度。</small></div>
          <label className="checkbox advanced-toggle"><input type="checkbox" checked={advancedMode} onChange={event => setAdvancedMode(event.target.checked)} />高级模式 <small>工作流选择与逐项参数覆盖</small></label>
          {advancedMode && <details className="advanced" open><summary><span><ChevronDown size={16} />高级设置</span><span>自动填充 / 参数覆盖</span></summary>
<div className="fields two">
            {(['image', 'video', 'reference'] as const).map(type => <div className="field" key={type}><label htmlFor={`workflow-${type}`}>{type === 'image' ? '关键帧图像工作流' : type === 'video' ? '视频工作流' : '初始参考图工作流'}</label><select id={`workflow-${type}`} value={advanced[`${type}_workflow_id`]} onChange={event => setAdvanced({ ...advanced, [`${type}_workflow_id`]: event.target.value })}><option value="">使用默认工作流</option>{workflows.data?.filter(item => type === 'reference' ? item.capability === 'TEXT_TO_IMAGE' : item.media_type === type).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>)}
            <div className="field"><label htmlFor="memory">显存模式</label><select id="memory" value={advanced.memory_mode} onChange={event => setAdvanced({ ...advanced, memory_mode: event.target.value })}><option value="auto">自动检测</option><option value="low">低显存</option><option value="manual">手动 / 标准预算</option></select></div>
            {([{ name: 'fps', label: 'FPS', min: config.data?.limits.fps.min, max: config.data?.limits.fps.max }, { name: 'seed', label: '随机种子', min: 0, max: 2147483647 }, { name: 'max_retries', label: '最大重试次数', min: 0, max: 5 }] as const).map(item => <div className="field" key={item.name}><label htmlFor={item.name}>{item.label}</label><input id={item.name} type="number" min={item.min} max={item.max} value={advanced[item.name]} onChange={event => setAdvanced({ ...advanced, [item.name]: Number(event.target.value) })} /></div>)}
          </div><label className="checkbox"><input type="checkbox" checked={advanced.qa_enabled} onChange={event => setAdvanced({ ...advanced, qa_enabled: event.target.checked })} />启用视觉质量检查{!config.data?.vlm_configured && <small>配置 VLM 后生效</small>}</label>{selectedWorkflows.map(workflow => <ParameterOverrides key={workflow.id} workflow={workflow} values={overrides[workflow.id] ?? {}} notify={notify} onChange={values => setOverrides(current => ({ ...current, [workflow.id]: values }))} />)}</details>}
        </div>
        <div className="generate-row"><p><span className="dot" />先预览分镜 · 确认后生成</p><button className="primary" disabled={busy || !idea.trim() || !workflows.data || !validDuration || missingAsset} type="submit"><Sparkles size={17} />{busy ? '正在创建…' : '生成分镜预览'}<ArrowRight size={17} /></button></div>
      </form>
      <aside className="creation-aside"><div className="director-note"><div className="note-top"><Clapperboard size={19} /><span>DIRECTOR'S NOTE</span></div><h2>你负责想象。<br />剩下的，交给导演。</h2><p>每一部短片，都从一个统一的视觉世界开始。</p><ol>{['规划一个完整故事', '预览并调整镜头与提示词', '确认后生成画面与视频', '检查、拼接，导出成片'].map((text, index) => <li key={text}><span>0{index + 1}</span>{text}</li>)}</ol><div className="frame-sketch" aria-hidden="true"><div className="sketch-sun" /><div className="sketch-hill a" /><div className="sketch-hill b" /><span>YOUR NEXT SCENE</span></div></div><div className="inspiration"><span className="small-label">没有灵感？从一个假设开始</span>{examples.map((example, index) => <button key={example} onClick={() => setIdea(example)}><span>0{index + 1}</span><p>{example}</p><ArrowUpRightSmall /></button>)}</div></aside>
    </div>
  </>;
}

function ArrowUpRightSmall() { return <ArrowRight size={15} className="diagonal" />; }
