import { useEffect, useState } from 'react';
import { ArrowUpRight, Clapperboard, FileText, Film, Layers3, Plus, Settings2, X } from 'lucide-react';
import { useResource } from './api';
import type { Settings } from './types';
import CreatePage from './CreatePage';
import { EpisodePage, EpisodesPage } from './EpisodePage';
import WorkflowsPage from './WorkflowsPage';
import SetupPage from './SetupPage';
import CreationPage from './CreationPage';

export type Notify = (message: string, error?: boolean) => void;
export const navigate = (page: string) => { window.location.hash = page; };

export default function App() {
  const [route, setRoute] = useState(window.location.hash.slice(1) || 'create');
  const [toast, setToast] = useState<{ message: string; error: boolean }>();
  const config = useResource<Settings>('/settings', 10000);
  useEffect(() => {
    const handler = () => setRoute(window.location.hash.slice(1) || 'create');
    window.addEventListener('hashchange', handler);
    return () => window.removeEventListener('hashchange', handler);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(undefined), 8000);
    return () => window.clearTimeout(timer);
  }, [toast]);
  const notify: Notify = (message, error = false) => setToast({ message, error });
  const menu = [
    { id: 'create', label: '开始创作', icon: Plus },
    { id: 'creation', label: '创作包', icon: FileText },
    { id: 'episodes', label: '我的短片', icon: Film },
    { id: 'workflows', label: '工作流', icon: Layers3 },
    { id: 'setup', label: '连接与设置', icon: Settings2 },
  ];
  return <div className="app-shell">
    <aside className="sidebar">
      <a href="#create" className="brand"><span className="brand-mark"><Clapperboard size={22} /></span><strong>AutoDirector<span>CREATIVE STUDIO</span></strong></a>
      <div className="workspace-label">创作工作台 <span>01</span></div>
      <nav aria-label="主导航">{menu.map(item => <a key={item.id} href={`#${item.id}`} className={route === item.id || (item.id === 'creation' && route.startsWith('creation/')) || (item.id === 'episodes' && route.startsWith('episode/')) ? 'active' : ''}><item.icon size={18} />{item.label}{item.id === 'create' && <span className="nav-plus">＋</span>}</a>)}</nav>
      <div className="sidebar-note"><span className="small-label">FROM IDEA TO FILM</span><p>让一个想法，<br />成为看得见的故事。</p><div className="film-lines"><i /><i /><i /><i /></div></div>
      <a href="#setup" className="connection-card"><span className={`dot ${config.error ? 'offline' : config.data ? 'online' : ''}`} /><div><strong>{config.error ? '后端尚未连接' : config.data ? '本地工作台已连接' : '正在连接工作台'}</strong><small>{config.error ? '检查本地服务' : 'ComfyUI · 创作方式'}</small></div><ArrowUpRight size={15} /></a>
    </aside>
    <main className="main">
      <header className="topbar"><span>STUDIO <span className="slash">/</span> {route.startsWith('creation/') ? '创作包详情' : route.startsWith('episode/') ? '短片详情' : menu.find(item => item.id === route)?.label ?? '开始创作'}</span><div><span className="local-pill"><i />LOCAL FIRST</span><span className="avatar">AD</span></div></header>
      <div className="page" key={route}>
        {route === 'creation' || route.startsWith('creation/') ? <CreationPage id={route.startsWith('creation/') ? route.slice(9) : undefined} notify={notify} /> : route === 'episodes' ? <EpisodesPage notify={notify} /> : route.startsWith('episode/') ? <EpisodePage id={route.slice(8)} notify={notify} /> : route === 'workflows' ? <WorkflowsPage notify={notify} /> : route === 'setup' ? <SetupPage notify={notify} onChange={config.refresh} /> : <CreatePage notify={notify} />}
      </div>
    </main>
    {toast && <div className={`toast ${toast.error ? 'error' : ''}`} role={toast.error ? 'alert' : 'status'}><span>{toast.message}</span><button className="icon-button" aria-label="关闭消息" onClick={() => setToast(undefined)}><X size={16} /></button></div>}
  </div>;
}
