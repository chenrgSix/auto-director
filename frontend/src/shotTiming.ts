// The editable video prompt is the source of truth; no separate saved timing state.
export const timingHeader = 'Shot timing (seconds):';
type Beat = { start: number; end: number; action: string };
export function readShotTiming(prompt: string): { overview: string; beats: Beat[] } | null {
  const parts = prompt.split(timingHeader);
  if (parts.length !== 2) return null;
  const beats: Beat[] = [];
  let cursor = 0;
  for (const line of parts[1].trim().split('\n')) {
    const match = /^(\d+(?:\.\d{1,2})?)-(\d+(?:\.\d{1,2})?)s: (\S.*)$/.exec(line.trim());
    if (!match) return null;
    const start = Number(match[1]), end = Number(match[2]);
    if (!Number.isFinite(end) || start !== cursor || end <= start) return null;
    beats.push({ start, end, action: match[3] });
    cursor = end;
  }
  return beats.length ? { overview: parts[0].trimEnd(), beats } : null;
}

export function timingIssue(prompt: string, duration: number): string | null {
  if (!prompt.includes(timingHeader)) return null;
  const timing = readShotTiming(prompt);
  if (!timing) return '动作时间线须从 0 开始、连续无重叠，格式为 0-3s: 动作。';
  if (Math.abs(timing.beats.at(-1)!.end - duration) > 0.001) return `动作时间线须覆盖本镜 ${duration} 秒。`;
  return null;
}

export function retimeShotPrompt(prompt: string, duration: number): string {
  const timing = readShotTiming(prompt);
  if (!timing || !Number.isFinite(duration) || duration <= 0) return prompt;
  const ratio = duration / timing.beats.at(-1)!.end;
  const scaled = timing.beats.map(b => ({ ...b, start: Math.round((b.start * ratio + Number.EPSILON) * 100) / 100, end: Math.round((b.end * ratio + Number.EPSILON) * 100) / 100 }));
  // A very small phase can collapse; leave it visible for manual correction instead of losing it.
  if (scaled.some(b => b.end <= b.start)) return prompt;
  return `${timing.overview}\n\n${timingHeader}\n${scaled.map(b => `${b.start}-${b.end}s: ${b.action}`).join('\n')}`;
}
