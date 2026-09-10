import { useEffect, useState } from 'react';
import { CheckCircle2, Cpu, Link2, Server, ShieldCheck } from 'lucide-react';
import { api, useResource } from './api';
import type { Notify } from './App';
import type { Job, Settings } from './types';
import { JobList } from './EpisodePage';
import { ErrorNotice, Loading } from './ui';

type Connection = { connected: boolean; node_count: number; system: { devices?: { name?: string; vram_total?: number; vram_free?: number }[] }; workflows: { id: string; name: string; validation: { valid: boolean; issues: { message?: string; code: string; class_type?: string; field?: string }[] } }[] };

export default function SetupPage({ notify, onChange }: { notify: Notify; onChange: () => void }) {
  const resource = useResource<Settings>('/settings');
  const jobs = useResource<Job[]>('/jobs', 3000);
  const [url, setUrl] = useState('http://127.0.0.1:8188');
  const [connection, setConnection] = useState<Connection>();
  const [problem, setProblem] = useState<string>();
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (resource.data) setUrl(resource.data.comfyui_url); }, [resource.data]);
  async function connect() {
    setBusy(true); setProblem(undefined); setConnection(undefined);
    try {
      await api('/settings', 'PATCH', { comfyui_url: url });
      onChange(); resource.refresh();
      const result = await api<Connection>('/comfyui/test', 'POST');
      setConnection(result); notify('已连接 ComfyUI，依赖检查完成');
    } catch (error) { setProblem((error as Error).message); }
    finally { setBusy(false); }
  }
  return <><div className="section-heading"><div><span className="eyebrow">SET UP YOUR STUDIO</span><h1>连接与设置</h1><p>接上渲染引擎和导演模型，就可以开始制作短片。</p></div><span className="local-pill"><ShieldCheck size={13} />密钥仅保存在后端</span></div>{resource.error && <ErrorNotice>{resource.error}</ErrorNotice>}{!resource.data ? <Loading /> : <div className="setup-grid"><section className="panel"><div className="panel-title"><Server size={19} />01 · ComfyUI 渲染引擎</div><p className="muted">使用你已有的 ComfyUI。工作台不会下载模型或安装节点。</p><div className="field"><label htmlFor="comfy-url">ComfyUI 地址</label><input id="comfy-url" type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="http://192.168.1.100:8188" /></div><button className="primary" disabled={busy || !url} onClick={() => void connect()}><Link2 size={16} />{busy ? '正在检测…' : '保存并检测连接'}</button>{problem && <ErrorNotice>{problem}</ErrorNotice>}{connection && <div className="connection-result"><div className="success-text"><CheckCircle2 size={16} />连接成功 · {connection.node_count} 种节点</div>{connection.system.devices?.map((device, index) => <div className="device" key={index}><Cpu size={20} /><div><strong>{device.name || '渲染设备'}</strong><small>可用 {((device.vram_free || 0) / 1024 ** 3).toFixed(1)} / 总计 {((device.vram_total || 0) / 1024 ** 3).toFixed(1)} GiB</small></div></div>)}{connection.workflows.map(workflow => <div className="dependency" key={workflow.id}><strong>{workflow.name}</strong><span className={workflow.validation.valid ? 'success-text' : 'error-text'}>{workflow.validation.valid ? '依赖检查通过' : `${workflow.validation.issues.length} 项待处理`}</span>{workflow.validation.issues.map((issue, index) => <p key={index}>{issue.class_type || issue.field || issue.code} · {issue.message || '查看工作流详情'}</p>)}</div>)}</div>}</section>
      <section className="panel"><div className="panel-title"><Cpu size={19} />02 · 导演与视觉模型</div><div className="model-status"><span>导演模型</span><strong>{resource.data.llm_model || '尚未配置'}</strong><span className={resource.data.llm_configured ? 'success-text' : 'muted'}>{resource.data.llm_configured ? '已配置 · 实际可用性在生成时验证' : '需要支持 JSON 输出的模型'}</span></div><div className="model-status"><span>视觉 QA 模型</span><strong>{resource.data.vlm_model || '尚未配置（可选）'}</strong><span className="muted">需要支持图像输入；未配置时会明确跳过视觉检查。</span></div><p className="muted">在仓库根目录的 <code>.env</code> 中配置兼容服务，然后重启后端。密钥不会发送给浏览器。</p><pre className="config-example">{'AD_LLM_BASE_URL=https://your-provider.example/v1\nAD_LLM_MODEL=your-director-model\nAD_LLM_API_KEY=your-key\nAD_VLM_MODEL=your-vision-model'}</pre><small className="muted">支持 OpenAI-compatible JSON / vision 接口。模型名称由你的服务提供方决定。</small></section>
      <section className="panel full-span"><div className="panel-title">03 · 开始生成前</div><div className="setup-steps"><div><span>01</span><strong>检查两个工作流</strong><p>确认图像与视频工作流的模型、节点和输入角色。</p></div><div><span>02</span><strong>先做一次试跑</strong><p>上传首尾帧，检查真实输出，再设为默认工作流。</p></div><div><span>03</span><strong>从 5 秒开始</strong><p>先验证一个短片，再逐步增加时长和质量。</p></div></div><a className="text-button" href="#workflows">前往工作流配置 →</a></section></div>}
    {!!jobs.data?.some(job => job.status === 'UNKNOWN') && <section className="panel"><div className="panel-title">需要核对的作业</div><p className="muted">提交响应丢失并不意味着没有执行。先核对服务端记录，再恢复任务。</p><JobList jobs={jobs.data.filter(job => job.status === 'UNKNOWN')} notify={notify} onChange={jobs.refresh} /></section>}
  </>;
}
