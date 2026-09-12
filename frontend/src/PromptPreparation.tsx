import { useEffect, useState } from 'react';
import { type Episode, type PreviewPromptField } from './types';

const labels: Record<PreviewPromptField, string> = { start_frame_prompt: '首帧提示词', end_frame_prompt: '尾帧提示词', video_prompt: '视频提示词与动作时间' };

export function PromptPreparation({ episode }: { episode: Episode }) {
  const [selected, setSelected] = useState<string>();
  const [clock, setClock] = useState(() => Date.now());
  const preparation = episode.prompt_preparation;
  const running = episode.status === 'PREPARING_PROMPTS' && preparation?.status === 'running';
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  const shot = episode.shots.find(item => item.id === selected) ?? episode.shots[0];
  if (!shot) return null;
  const current = running ? episode.shots.filter(item => preparation.active_shot_ids.includes(item.id)) : [];
  const seconds = preparation?.batch_started_at ? Math.max(0, Math.floor((clock - Date.parse(preparation.batch_started_at)) / 1000)) : 0;
  const last = preparation?.recent_batches.at(-1);
  const fields = (Object.keys(labels) as PreviewPromptField[]).filter(field => field !== 'end_frame_prompt' || preparation?.video_capability !== 'IMAGE_TO_VIDEO');
  return <section className="panel prompt-preparation">
    <div className="panel-title">分镜准备 · 可提前查看</div>
    <p className="muted">故事与时长已列在下面，提示词每批保存后自动显示。这里展示生成草稿；全部准备完成后，可编辑分镜并查看高级覆盖后的实际输入，再确认生成。</p>
    {current.length > 0 && <p role="status">正在编写第 {current.map(item => item.index + 1).join('、')} 镜 · 已等待 {seconds} 秒</p>}
    {preparation && <p className="muted">本次已保存 {preparation.batches} 批 · 已结束的模型请求 {preparation.requests} 次{last && ` · 最近一批${last.status === 'completed' ? '用时' : '中断于'} ${Math.round(last.elapsed_seconds)} 秒`}</p>}
    <div className="preparation-grid">
      <div className="preparation-shots" aria-label="准备中的镜头列表">
        {episode.shots.map(item => <button key={item.id} aria-pressed={item.id === shot.id} onClick={() => setSelected(item.id)}>
          <strong>{item.index + 1}. {item.title}</strong><small>{item.duration.toFixed(2)} 秒 · {item.prompts ? '提示词已保存' : current.some(active => active.id === item.id) ? '正在编写' : '等待编写'}</small>
        </button>)}
      </div>
      <div className="preparation-detail">
        <h3>第 {shot.index + 1} 镜 · {shot.title}</h3><p>{shot.duration.toFixed(2)} 秒 · {shot.action}</p><p className="muted">镜头：{shot.camera}</p>
        {shot.prompts ? <>{fields.map(field => <details key={field} open={field === 'video_prompt'}><summary>{labels[field]}</summary><p className="preparation-prompt">{shot.prompts?.[field]}</p></details>)}{shot.prompts.narration_text && <p>旁白：{shot.prompts.narration_text}</p>}</> : <p className="muted">本镜提示词尚未保存。已完成的镜头会保留，可中断后继续准备。</p>}
      </div>
    </div>
  </section>;
}
