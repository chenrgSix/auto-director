import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { CheckCircle2, Cpu, Link2, Save, Server, ShieldCheck, SlidersHorizontal } from 'lucide-react';
import { api, useResource } from './api';
import type { Notify } from './App';
import type { Job, Settings } from './types';
import { JobList } from './EpisodePage';
import { ErrorNotice, Loading } from './ui';
import { ModelTests } from './ModelTests';

type Connection = { connected: boolean; node_count: number; system: { devices?: { name?: string; vram_total?: number; vram_free?: number }[] }; workflows: { id: string; name: string; validation: { valid: boolean; issues: { message?: string; code: string; class_type?: string; field?: string }[] } }[] };
const editable = ['comfyui_url', 'allow_public_comfyui', 'llm_base_url', 'llm_model', 'vlm_model', 'llm_timeout', 'prompt_batch_size', 'render_timeout', 'request_timeout', 'max_asset_mb', 'poll_interval'] as const;
type Draft = Pick<Settings, typeof editable[number]>;
const limits = [
  { key: 'render_timeout', label: '渲染等待上限（秒）', min: 1, max: 14400, step: 1 },
  { key: 'request_timeout', label: 'ComfyUI 请求超时（秒）', min: 1, max: 120, step: 1 },
  { key: 'max_asset_mb', label: '单个资产上限（MiB）', min: 1, max: 2048, step: 1 },
  { key: 'poll_interval', label: '渲染进度查询间隔（秒）', min: 0.01, max: 30, step: 0.01 },
] as const;

export default function SetupPage({ notify, onChange }: { notify: Notify; onChange: () => void }) {
  const resource = useResource<Settings>('/settings');
  const jobs = useResource<Job[]>('/jobs', 3000);
  const [saved, setSaved] = useState<Settings>();
  const [draft, setDraft] = useState<Draft>();
  const [keyValue, setKeyValue] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [connection, setConnection] = useState<Connection>();
  const [problem, setProblem] = useState<string>();
  const [savedMessage, setSavedMessage] = useState(false);
  const [busy, setBusy] = useState<'save' | 'test' | null>(null);
  const [modelTestVersion, setModelTestVersion] = useState(0);
  const [modelTesting, setModelTesting] = useState(false);

  useEffect(() => {
    if (resource.data) { setSaved(resource.data); setDraft(resource.data); }
  }, [resource.data]);

  const dirty = !!draft && !!saved && (editable.some(key => draft[key] !== saved[key]) || !!keyValue.trim() || clearKey);
  function change<K extends keyof Draft>(key: K, value: Draft[K]) {
    setModelTestVersion(value => value + 1);
    setDraft(current => current ? { ...current, [key]: value } : current);
    setSavedMessage(false);
    setProblem(undefined);
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!draft || !saved || !dirty || modelTesting) return;
    setBusy('save'); setProblem(undefined); setSavedMessage(false);
    const changes: Record<string, unknown> = {};
    for (const key of editable) if (draft[key] !== saved[key]) changes[key] = draft[key];
    if (keyValue.trim()) changes.llm_api_key = keyValue.trim();
    if (clearKey) changes.clear_llm_api_key = true;
    try {
      const updated = await api<Settings>('/settings', 'PATCH', changes);
      setSaved(updated); setDraft(updated); setKeyValue(''); setClearKey(false);
      setModelTestVersion(value => value + 1);
      setConnection(undefined); setSavedMessage(true); onChange();
      notify('配置已保存并生效');
    } catch (error) { setProblem((error as Error).message); }
    finally { setBusy(null); }
  }

  async function connect() {
    setBusy('test'); setProblem(undefined); setConnection(undefined);
    try {
      setConnection(await api<Connection>('/comfyui/test', 'POST'));
      notify('已连接 ComfyUI，依赖检查完成');
    } catch (error) { setProblem((error as Error).message); }
    finally { setBusy(null); }
  }

  return <>
    <div className="section-heading">
      <div><span className="eyebrow">SET UP YOUR STUDIO</span><h1>连接与设置</h1><p>在这里配置渲染引擎和模型，保存后立即生效。</p></div>
      <span className="local-pill"><ShieldCheck size={13} />密钥保存后不回显</span>
    </div>
    {resource.error && <ErrorNotice>{resource.error}</ErrorNotice>}
    {!draft || !saved ? (!resource.error && <Loading />) : <form onSubmit={event => void save(event)}>
      <fieldset className="setup-grid settings-fields" disabled={!!busy}>
        <section className="panel">
          <div className="panel-title"><Server size={19} />01 · ComfyUI 渲染引擎</div>
          <p className="muted">连接已有的 ComfyUI。工作台不会下载模型或安装节点。</p>
          <div className="field">
            <label htmlFor="comfy-url">ComfyUI 地址</label>
            <input id="comfy-url" type="url" required maxLength={500} value={draft.comfyui_url} onChange={event => change('comfyui_url', event.target.value)} placeholder="http://192.168.1.100:8188" />
            <small>填写服务根地址，不包含路径或查询参数。</small>
          </div>
          <label className="checkbox"><input type="checkbox" checked={draft.allow_public_comfyui} onChange={event => change('allow_public_comfyui', event.target.checked)} />允许公网 ComfyUI 地址</label>
          <div className="settings-test">
            <button type="button" disabled={!!busy || dirty || modelTesting} onClick={() => void connect()}><Link2 size={16} />{busy === 'test' ? '正在检测…' : '检测 ComfyUI'}</button>
            {dirty && <small className="muted">先保存更改，再检测连接。</small>}
          </div>
          {connection && <div className="connection-result">
            <div className="success-text"><CheckCircle2 size={16} />连接成功 · {connection.node_count} 种节点</div>
            {connection.system.devices?.map((device, index) => <div className="device" key={index}><Cpu size={20} /><div><strong>{device.name || '渲染设备'}</strong><small>可用 {((device.vram_free || 0) / 1024 ** 3).toFixed(1)} / 总计 {((device.vram_total || 0) / 1024 ** 3).toFixed(1)} GiB</small></div></div>)}
            {connection.workflows.map(workflow => <div className="dependency" key={workflow.id}><strong>{workflow.name}</strong><span className={workflow.validation.valid ? 'success-text' : 'error-text'}>{workflow.validation.valid ? '依赖检查通过' : `${workflow.validation.issues.length} 项待处理`}</span>{workflow.validation.issues.map((issue, index) => <p key={index}>{issue.class_type || issue.field || issue.code} · {issue.message || '查看工作流详情'}</p>)}</div>)}
          </div>}
        </section>
        <section className="panel">
          <div className="panel-title"><Cpu size={19} />02 · 导演与视觉模型</div>
          <p className="muted">使用支持 JSON / vision 的 OpenAI-compatible 服务。</p>
          <div className="field">
            <label htmlFor="llm-base-url">模型 API 端点</label>
            <input id="llm-base-url" type="url" required maxLength={500} value={draft.llm_base_url} onChange={event => change('llm_base_url', event.target.value)} placeholder="https://your-provider.example/v1" />
            <small>通常以 /v1 结尾，不要附加 /chat/completions。</small>
          </div>
          <div className="field">
            <label htmlFor="llm-model">导演模型名称</label>
            <input id="llm-model" maxLength={200} value={draft.llm_model} onChange={event => change('llm_model', event.target.value)} placeholder="服务提供方的模型名称" />
          </div>
          <div className="field">
            <label htmlFor="vlm-model">视觉 QA 模型名称（可选）</label>
            <input id="vlm-model" maxLength={200} value={draft.vlm_model} onChange={event => change('vlm_model', event.target.value)} placeholder="支持图像输入的模型，留空跳过视觉 QA" />
            <small>视觉模型共用上方端点和密钥；可通过下方按钮测试图像输入。</small>
          </div>
          <div className="field">
            <label htmlFor="llm-api-key">API 密钥</label>
            <input id="llm-api-key" type="password" autoComplete="new-password" maxLength={4096} value={keyValue} disabled={clearKey || !!busy} onChange={event => { setKeyValue(event.target.value); setSavedMessage(false); setProblem(undefined); setModelTestVersion(value => value + 1); }} placeholder={saved.llm_api_key_configured ? '已保存密钥，留空保持不变' : '无需认证的服务可留空'} />
            <small>{saved.llm_api_key_configured ? '后端已有密钥。输入新值可替换，保存后输入框会清空。' : '密钥仅存储在本机后端，不会通过读取接口返回。'}</small>
            {saved.llm_api_key_configured && <label className="checkbox"><input type="checkbox" checked={clearKey} onChange={event => { setClearKey(event.target.checked); setKeyValue(''); setSavedMessage(false); setModelTestVersion(value => value + 1); }} />清除已保存的密钥</label>}
          </div>
          <div className="field">
            <label htmlFor="llm-timeout">模型请求超时（秒）</label>
            <input id="llm-timeout" type="number" required min={1} max={3600} step={1} value={draft.llm_timeout} onChange={event => change('llm_timeout', Number(event.target.value))} />
            <small>默认 600 秒，可设置 1～3600 秒；导演、视觉模型和模型测试共用。</small>
          </div>
          <ModelTests key={modelTestVersion} settings={saved} disabled={!!busy} dirty={dirty} onPending={setModelTesting} />
          {typeof saved.prompt_batch_size === 'number' && <div className="field">
            <label htmlFor="prompt-batch-size">每次准备镜头数</label>
            <select id="prompt-batch-size" value={draft.prompt_batch_size} onChange={event => change('prompt_batch_size', Number(event.target.value))}>
              <option value={3}>最多 3 镜</option><option value={2}>最多 2 镜</option><option value={1}>逐镜准备（兼容模式）</option>
            </select>
            <small>相邻镜头共享一次模型请求，按顺序保持故事连续。内容过长时自动缩小批次；模型不适应时可切回逐镜准备，已有分镜保留。</small>
          </div>}
        </section>
        <details className="panel full-span settings-advanced">
          <summary><SlidersHorizontal size={16} />运行参数</summary>
          <div className="fields two">
            {limits.map(limit => <div className="field" key={limit.key}>
              <label htmlFor={limit.key}>{limit.label}</label>
              <input id={limit.key} type="number" required min={limit.min} max={limit.max} step={limit.step} value={draft[limit.key]} onChange={event => change(limit.key, Number(event.target.value))} />
            </div>)}
          </div>
        </details>
        <div className="full-span settings-save">
          {problem && <ErrorNotice>{problem}</ErrorNotice>}
          {savedMessage && <div className="success-text" role="status"><CheckCircle2 size={16} />配置已保存并生效，无需重启。</div>}
          <div className="settings-save-row"><p className="muted">{dirty ? '有尚未保存的更改。' : '页面配置会保留到下次启动。'} 任务执行中需等待结束后再保存。</p><button type="submit" className="primary" disabled={!!busy || !dirty || modelTesting}><Save size={16} />{busy === 'save' ? '正在保存…' : '保存配置'}</button></div>
        </div>
        <section className="panel full-span">
          <div className="panel-title">03 · 开始生成前</div>
          <div className="setup-steps"><div><span>01</span><strong>检查两个工作流</strong><p>确认图像与视频工作流的模型、节点和输入角色。</p></div><div><span>02</span><strong>先做一次试跑</strong><p>上传首尾帧，检查真实输出，再设为默认工作流。</p></div><div><span>03</span><strong>从 5 秒开始</strong><p>先验证一个短片，再逐步增加时长和质量。</p></div></div>
          <a className="text-button" href="#workflows">前往工作流配置 →</a>
        </section>
      </fieldset>
    </form>}
    {!!jobs.data?.some(job => job.status === 'UNKNOWN') && <section className="panel"><div className="panel-title">需要核对的作业</div><p className="muted">提交响应丢失并不意味着没有执行。先核对服务端记录，再恢复任务。</p><JobList jobs={jobs.data.filter(job => job.status === 'UNKNOWN')} notify={notify} onChange={jobs.refresh} /></section>}
  </>;
}
