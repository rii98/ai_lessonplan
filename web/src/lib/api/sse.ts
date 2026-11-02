/** One Server-Sent Event frame. `data` is parsed JSON when possible. */
export interface SseEvent<T = unknown> {
  event: string;
  data: T;
}

export function parseSseFrame(frame: string): SseEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  if (!data.length) return null;
  const raw = data.join("\n");
  try {
    return { event, data: JSON.parse(raw) };
  } catch {
    return { event, data: raw };
  }
}

/** Read an SSE body (a POST response — EventSource can't send credentials/bodies). */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const f of frames) {
        const ev = parseSseFrame(f);
        if (ev) yield ev;
      }
    }
    const tail = parseSseFrame(buffer);
    if (tail) yield tail;
  } finally {
    reader.releaseLock();
  }
}
