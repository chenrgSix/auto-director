import { useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { ArrowLeft, Download, FileText, Plus, Save, Upload } from 'lucide-react';
import { api, useResource } from './api';
import { navigate, type Notify } from './App';
import { STATUS, type Workflow } from './types';
import { ErrorNotice, Loading } from './ui';

type Brief = {
  idea: string; target_duration: number; aspect_ratio: string; style: string;
  image_review_required?: boolean;
  quality: string; visual_review: string; seed: number;
  image_workflow_id: string | null; video_workflow_id: string | null;
  reference_workflow_id: string | null; max_shot_duration: number | null;
};
type Document = {
  format: string; brief: Brief; title: string; logline: string; bible: unknown;
  shots: { id: string; index: number; title: string; duration: number; action: string; prompts: unknown }[];
  decisions: string[]; open_questions: string[]; notes: string;
};
type ProjectSummary = { id: string; title: string; revision: number; updated_at: string };
type Production = { episode_id: string; revision: number; title: string; status: string; version: number };
type Project = ProjectSummary & {
  document: Document; history: { revision: number; content_hash: string; created_at: string }[];
  productions: Production[];
};
type Report = { warnings?: { shot_index: number; message: string }[]; valid: boolean; revision: number; constraints_hash: string | null; issues: { code: string; message: string; details?: unknown }[] };
type Saved = { project_id: string; revision: number };
const root = '/creation/projects';

function emptyDocument(idea: string, duration: number): Document {
  return {
    format: 'autodirector.creation/v1',
    brief: { idea, target_duration: duration, aspect_ratio: '9:16', style: '自然纪录片', quality: 'standard',
      image_review_required: true, visual_review: 'manual', seed: 42, image_workflow_id: null, video_workflow_id: null,
      reference_workflow_id: null, max_shot_duration: null },
    title: '', logline: '', bible: null, shots: [], decisions: [], open_questions: [], notes: '',
  };
}

function download(name: string, content: unknown, raw = false) {
  const url = URL.createObjectURL(new Blob([raw ? String(content) : JSON.stringify(content, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a'); link.href = url; link.download = name;
  document.body.appendChild(link); link.click(); link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function readFile(event: ChangeEvent<HTMLInputElement>) {
  const file = event.target.files?.[0]; event.target.value = '';
  if (!file) return;
  if (file.size > 1500000) throw new Error('创作包须小于 1.5 MB');
  let value: Document;
  try { value = JSON.parse(await file.text()) as Document; }
  catch { throw new Error('文件不是有效的 JSON，请导入 AutoDirector 创作包'); }
  if (!value || value.format !== 'autodirector.creation/v1' || !value.brief || !Array.isArray(value.shots)) {
    throw new Error('文件缺少创作包格式标识、创作要求或分镜列表');
  }
  return await api<Document>('/creation/normalize', 'POST', value);
}

export default function CreationPage({ id, notify }: { id?: string; notify: Notify }) {
  return id ? <ProjectLoader id={id} notify={notify} /> : <ProjectList notify={notify} />;
}

function ProjectList({ notify }: { notify: Notify }) {
  const projects = useResource<ProjectSummary[]>(root, 4000);
  const [idea, setIdea] = useState('');
  const [duration, setDuration] = useState(10);
  const [busy, setBusy] = useState(false);
  const pending = useRef<{ serialized: string; request_id: string } | null>(null);
  async function create(value: Document) {
    if (busy) return;
    setBusy(true);
    try {
      const serialized = JSON.stringify(value);
      if (pending.current?.serialized !== serialized) pending.current = { serialized, request_id: crypto.randomUUID() };
      const result = await api<Saved>(root, 'POST', { request_id: pending.current.request_id, document: value });
      pending.current = null; navigate(`creation/${result.project_id}`); notify('创作项目已保存，可以连接 Codex 或导入创作成果');
    } catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); }
  }
  async function importFile(event: ChangeEvent<HTMLInputElement>) {
    try { const value = await readFile(event); if (value) await create(value); }
    catch (error) { notify((error as Error).message, true); }
  }
  return <>
    <div className="section-heading"><div><span className="eyebrow">YOUR CREATIVE WORKSPACE</span><h1>创作包</h1><p>在自己的 Codex 会话中打磨故事，把确认的版本交给 AutoDirector 制作。</p></div></div>
    <div className="creation-start">
      <form className="panel" onSubmit={(event: FormEvent) => { event.preventDefault(); void create(emptyDocument(idea, duration)); }}>
        <div className="panel-title"><Plus size={18} />新建创作项目</div>
        <div className="field"><label htmlFor="creation-idea">你想讲一个什么故事？</label><textarea id="creation-idea" required maxLength={2000} value={idea} onChange={event => setIdea(event.target.value)} placeholder="写下想法、人物或你希望保留的情节" /></div>
        <div className="field"><label htmlFor="creation-duration">目标时长（秒）</label><input id="creation-duration" type="number" required min={1} max={600} step="0.01" value={duration} onChange={event => setDuration(Number(event.target.value))} /></div>
        <button className="primary" disabled={busy || !idea.trim()}><Plus size={16} />建立创作包</button>
      </form>
      <section className="panel creation-intro"><FileText size={28} /><h2>已经写好了？</h2><p>导入创作包作为新项目。可以先保存草稿，补齐设定和分镜后再校验、预览。</p>
        <label className={`creation-upload ${busy ? 'disabled' : ''}`}><Upload size={16} />导入为新项目<input type="file" accept=".json,application/json" disabled={busy} onChange={event => void importFile(event)} /></label>
        <p className="muted">这里不需要文字模型 API Key。图片与视频仍由你配置的 ComfyUI 制作。</p>
      </section>
    </div>
    <div className="section-heading"><h2>我的创作项目</h2></div>
    {projects.error && <ErrorNotice>{projects.error}</ErrorNotice>}
    {!projects.data ? <Loading /> : <div className="creation-projects">{projects.data.map(project => <a className="panel creation-project-card" key={project.id} href={`#creation/${project.id}`}><FileText size={22} /><div><h3>{project.title}</h3><p>版本 {project.revision} · {new Date(project.updated_at).toLocaleString('zh-CN')}</p></div><span>继续创作 →</span></a>)}{!projects.data.length && <p className="muted">建立第一个创作包，开始整理你的故事。</p>}</div>}
  </>;
}

function ProjectLoader({ id, notify }: { id: string; notify: Notify }) {
  const resource = useResource<Project>(`${root}/${id}`, 3000);
  if (!resource.data) return resource.error ? <ErrorNotice>{resource.error}</ErrorNotice> : <Loading />;
  return <ProjectEditor key={id} project={resource.data} notify={notify} refresh={resource.refresh} disconnected={!!resource.error} />;
}

function ProjectEditor({ project, notify, refresh, disconnected }: { project: Project; notify: Notify; refresh: () => void; disconnected: boolean }) {
  const [base, setBase] = useState(project);
  const [draft, setDraft] = useState(project.document);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<Report>();
  const [jsonText, setJsonText] = useState<string>();
  const [discard, setDiscard] = useState<'reload' | 'json'>();
  const workflows = useResource<Workflow[]>('/workflows');
  const pendingSave = useRef<{ serialized: string; request_id: string } | null>(null);
  const pendingSubmit = useRef<{ expected_revision: number; constraints_hash: string; request_id: string } | null>(null);
  if (project.revision > base.revision && !dirty && jsonText === undefined) {
    setBase(project); setDraft(project.document); setJsonText(undefined); setReport(undefined);
  }
  const stale = project.revision > base.revision;
  const path = `${root}/${project.id}`;
  function reload() { setBase(project); setDraft(project.document); setDirty(false); setReport(undefined); setJsonText(undefined); setDiscard(undefined); }
  function edit(values: Partial<Document>) { setDraft(current => ({ ...current, ...values })); setDirty(true); setReport(undefined); setJsonText(undefined); }
  function editBrief(values: Partial<Brief>) { edit({ brief: { ...draft.brief, ...values } }); }
  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    try { await action(); }
    catch (error) { notify((error as Error).message, true); }
    finally { setBusy(false); refresh(); }
  }
  async function save(value = draft) {
    const body = { expected_revision: base.revision, document: value };
    const serialized = JSON.stringify(body);
    if (pendingSave.current?.serialized !== serialized) pendingSave.current = { serialized, request_id: crypto.randomUUID() };
    await api<Saved>(`${path}/revisions`, 'POST', { ...body, request_id: pendingSave.current.request_id });
    pendingSave.current = null;
    const latest = await api<Project>(path);
    setBase(latest); setDraft(latest.document); setDirty(false); setJsonText(undefined); setReport(undefined);
    notify('新版本已保存，已交付的制作保持原版本');
  }
  async function importRevision(event: ChangeEvent<HTMLInputElement>) {
    try { const value = await readFile(event); if (value) { setDraft(value); setDirty(true); setReport(undefined); setJsonText(undefined); notify('创作包已载入编辑区，保存后成为新版本'); } }
    catch (error) { notify((error as Error).message, true); }
  }
  async function validate() {
    const checked = await api<Report>(`${path}/validate`, 'POST');
    setReport(checked);
    notify(checked.valid ? '制作技术校验通过，可以提交分镜预览' : '创作包还有待补充或修正的内容', !checked.valid);
  }
  async function submitPreview() {
    if (!report?.valid || !report.constraints_hash) return;
    if (pendingSubmit.current?.expected_revision !== report.revision || pendingSubmit.current.constraints_hash !== report.constraints_hash) {
      pendingSubmit.current = { expected_revision: report.revision, constraints_hash: report.constraints_hash, request_id: crypto.randomUUID() };
    }
    const delivery = await api<{ episode_id: string }>(`${path}/submit`, 'POST', pendingSubmit.current);
    pendingSubmit.current = null; navigate(`episode/${delivery.episode_id}`); notify('分镜预览已准备，确认后才开始制作');
  }
  return <>
    <a className="back-link" href="#creation"><ArrowLeft size={15} />全部创作包</a>
    <div className="section-heading"><div><span className="eyebrow">CREATION PACKAGE · VERSION {base.revision}</span><h1>{base.title}</h1><p>持续打磨创作包；每次交付都保留一份独立的制作快照。</p></div><div className="actions">{dirty || jsonText !== undefined ? <button disabled={busy} onClick={() => download(`creation-${project.id}-v${base.revision}-draft.json`, jsonText ?? draft, jsonText !== undefined)}><Download size={15} />导出当前草稿</button> : <a className="creation-download" download href={`/api/v1${path}/export?revision=${base.revision}&download=true`}><Download size={15} />导出创作包</a>}<button disabled={busy} onClick={() => void run(async () => { await navigator.clipboard.writeText(jsonText ?? JSON.stringify(draft, null, 2)); notify('当前创作内容已复制'); })}>复制 JSON</button></div></div>
    {disconnected && <ErrorNotice>服务暂时无法连接，当前未保存的内容仍在编辑区。</ErrorNotice>}
    {stale && <ErrorNotice>Codex 或其他页面已经保存了版本 {project.revision}。当前修改已保留，请先导出草稿，再载入最新版本合并。<button disabled={busy} onClick={() => dirty || jsonText !== undefined ? setDiscard('reload') : reload()}>载入最新版本</button></ErrorNotice>}
    {discard && <div className="notice" role="alert"><p>{discard === 'reload' ? '载入最新版本将替换本页未保存的内容。请先导出或复制需要保留的草稿。' : '将放弃尚未应用的 JSON 编辑，保留当前编辑区中的创作包。'}</p><button onClick={() => { if (discard === 'reload') reload(); else { setJsonText(undefined); setDiscard(undefined); } }}>确认放弃未保存内容</button><button onClick={() => setDiscard(undefined)}>保留编辑</button></div>}
    <section className="panel creation-connect"><div><strong>与自己的 Codex 一起创作</strong><p>在 Codex 设置中添加 MCP 服务：<code>{window.location.port === '5173' ? 'http://127.0.0.1:8000/mcp/' : `${window.location.origin}/mcp/`}</code></p><p className="muted">连接后让 Codex 读取此项目、讨论修改并保存新版本。关闭会话不会取消已开始的制作。</p></div><button onClick={() => void run(async () => { await navigator.clipboard.writeText(`使用 AutoDirector MCP 读取创作项目 ${project.id} 的上下文与制作约束。请与我一起打磨剧本、视觉设定和分镜提示词，并保存创作包新版本。先校验再提交预览；开始制作前按我的授权执行。`); notify('创作指令已复制，可粘贴到自己的 Codex 会话'); })}>复制创作指令</button><a className="creation-download" download aria-disabled={busy || dirty || jsonText !== undefined} href={busy || dirty || jsonText !== undefined ? undefined : `/api/v1${path}/context?download=true`}>导出创作上下文</a></section>
    <div className="creation-editor-layout"><section className="panel"><fieldset disabled={busy || jsonText !== undefined}>
      <div className="panel-title">故事与创作要求<span className="muted">{dirty ? '有未保存的修改' : `已保存 · v${base.revision}`}</span></div>
      <div className="field"><label htmlFor="package-title">片名</label><input id="package-title" value={draft.title ?? ''} maxLength={200} onChange={e => edit({ title: e.target.value })} /></div>
      <div className="field"><label htmlFor="package-idea">创作要求</label><textarea id="package-idea" value={draft.brief.idea} maxLength={2000} onChange={e => editBrief({ idea: e.target.value })} /></div>
      <div className="field"><label htmlFor="package-logline">故事梗概</label><textarea id="package-logline" value={draft.logline ?? ''} maxLength={2000} onChange={e => edit({ logline: e.target.value })} /></div>
      <div className="fields two"><div className="field"><label htmlFor="package-duration">目标时长（秒）</label><input id="package-duration" type="number" min={1} max={600} step="0.01" value={draft.brief.target_duration} onChange={e => editBrief({ target_duration: Number(e.target.value) })} /></div><div className="field"><label htmlFor="package-ratio">画幅</label><select id="package-ratio" value={draft.brief.aspect_ratio ?? '9:16'} onChange={e => editBrief({ aspect_ratio: e.target.value })}>{['9:16', '16:9', '1:1'].map(value => <option key={value}>{value}</option>)}</select></div></div>
      <div className="field"><label htmlFor="package-style">视觉风格</label><input id="package-style" value={draft.brief.style ?? '自然纪录片'} maxLength={200} onChange={e => editBrief({ style: e.target.value })} /></div>
      <div className="field"><label htmlFor="package-notes">创作备忘</label><textarea id="package-notes" value={draft.notes ?? ''} maxLength={10000} onChange={e => edit({ notes: e.target.value })} placeholder="已经确认的方向、修改理由或希望 Codex 继续处理的问题" /></div>
      {!!draft.decisions?.length && <details><summary>已记录的创作决定</summary><ul>{draft.decisions.map((text, index) => <li key={index}>{text}</li>)}</ul></details>}
      {!!draft.open_questions?.length && <details open><summary>待讨论问题</summary><ul>{draft.open_questions.map((text, index) => <li key={index}>{text}</li>)}</ul></details>}
    </fieldset></section><section className="panel">
      <div className="panel-title">制作与交付</div><p className="muted">可以先保存草稿。设定与提示词完整、通过当前工作流校验后，再提交分镜预览。</p>
      <fieldset disabled={busy || jsonText !== undefined}>{(['image', 'video', 'reference'] as const).map(kind => <div className="field" key={kind}><label htmlFor={`package-${kind}`}>{kind === 'image' ? '关键帧工作流' : kind === 'video' ? '视频工作流' : '参考图工作流'}</label><select id={`package-${kind}`} value={draft.brief[`${kind}_workflow_id`] ?? ''} onChange={e => editBrief({ [`${kind}_workflow_id`]: e.target.value || null })}><option value="">使用当前默认工作流</option>{workflows.data?.filter(item => kind === 'reference' ? item.capability === 'TEXT_TO_IMAGE' : item.media_type === kind).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>)}
      <label className="checkbox"><input type="checkbox" checked={draft.brief.image_review_required ?? false} onChange={e => editBrief({ image_review_required: e.target.checked })} />生成视频前确认参考图和关键帧</label>
      <div className="field"><label htmlFor="package-review">画面复核</label><select id="package-review" value={draft.brief.visual_review ?? 'manual'} onChange={e => editBrief({ visual_review: e.target.value })}><option value="manual">由我和 Codex 查看实际画面</option><option value="model">使用已配置的视觉模型</option></select><small>媒体完整性和时长始终检查；制作技术校验不代表剧情或画面质量已通过。</small></div></fieldset>
      <div className="actions creation-action-stack"><button className="primary" disabled={busy || !dirty || jsonText !== undefined || stale || disconnected} onClick={() => void run(() => save())}><Save size={15} />保存新版本</button><button disabled={busy || dirty || jsonText !== undefined || stale || disconnected} onClick={() => void run(validate)}>校验创作包</button><button disabled={busy || dirty || jsonText !== undefined || stale || disconnected || !report?.valid} onClick={() => void run(submitPreview)}>提交分镜预览</button></div>
      {report && <div className={`notice ${report.valid ? 'creation-valid' : ''}`} role="status"><strong>{report.valid ? `版本 ${report.revision} 制作技术校验通过` : '请补充或修正以下内容'}</strong>{!!report.warnings?.length && <details><summary>参考素材与连续性提示 · {report.warnings.length}</summary>{report.warnings.map((warning, index) => <p key={index}>第 {warning.shot_index + 1} 镜：{warning.message}</p>)}</details>}{report.issues.map((issue, index) => <div key={index}><p>{issue.message}</p>{issue.details != null && <details><summary>查看具体位置与约束</summary><pre>{JSON.stringify(issue.details, null, 2)}</pre></details>}</div>)}</div>}
      <label className="creation-upload"><Upload size={15} />载入修改后的创作包<input type="file" disabled={busy || jsonText !== undefined} accept=".json,application/json" onChange={e => void importRevision(e)} /></label>
    </section></div>
    <section className="panel"><div className="panel-title">分镜草稿<span className="muted">{draft.shots.length} 镜 · {draft.shots.reduce((sum, shot) => sum + shot.duration, 0).toFixed(2)} 秒</span></div>{!draft.shots.length ? <p className="muted">尚未编写分镜。让 Codex 获取创作上下文，完成后保存新版本；也可以载入已有创作包。</p> : <div className="creation-shot-list">{draft.shots.map((shot, index) => <article key={`${shot.id}-${index}`}><span className="small-label">镜头 {shot.index + 1} · {shot.duration}s</span><h3>{shot.title}</h3><p>{shot.action}</p><small className="muted">{shot.prompts ? '已有制作提示词' : '待补充制作提示词'}</small></article>)}</div>}</section>
    {draft.bible != null && <details className="panel creation-json"><summary>视觉设定</summary><pre>{JSON.stringify(draft.bible, null, 2)}</pre></details>}
    <details className="panel creation-json"><summary>高级：编辑完整创作包</summary><p className="muted">用于修改视觉设定、分镜及提示词。应用到编辑区后，保存为新版本。</p><textarea disabled={busy} aria-label="完整创作包 JSON" spellCheck={false} value={jsonText ?? JSON.stringify(draft, null, 2)} onChange={e => { setJsonText(e.target.value); }} />{jsonText !== undefined && <p className="notice">完整创作包有未应用的编辑，请先应用到编辑区，再保存新版本。</p>}<button disabled={busy || jsonText === undefined} onClick={() => void run(async () => { const value = await api<Document>('/creation/normalize', 'POST', JSON.parse(jsonText!)); setDraft(value); setDirty(true); setReport(undefined); setJsonText(undefined); notify('已应用到编辑区，请保存新版本'); })}>应用到编辑区</button>{jsonText !== undefined && <button disabled={busy} onClick={() => setDiscard('json')}>放弃 JSON 编辑</button>}</details>
    <div className="creation-editor-layout"><section className="panel"><div className="panel-title">创作版本</div>{project.history.map(item => <div className="creation-history-row" key={item.revision}><span>版本 {item.revision}<small>{new Date(item.created_at).toLocaleString('zh-CN')}</small></span><a className="creation-download" download href={`/api/v1${path}/export?revision=${item.revision}&download=true`}><Download size={14} />导出此版本</a></div>)}</section>
      <section className="panel"><div className="panel-title">已交付制作</div>{project.productions.length ? project.productions.map(item => <a className="creation-history-row" key={item.episode_id} href={`#episode/${item.episode_id}`}><span>{item.title}<small>创作版本 {item.revision}</small></span><span>{STATUS[item.status] ?? item.status} →</span></a>) : <p className="muted">尚未提交。提交后先预览确认，再开始图片与视频生成。</p>}</section></div>
  </>;
}
