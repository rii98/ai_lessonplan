import { useEffect, useRef } from "react";
import { SeamlessText } from "./Field";
import { Plus, X } from "./icons";
import s from "./Lists.module.css";

export interface LineListProps {
  values: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  addLabel?: string;
  readOnly?: boolean;
  numbered?: boolean;
  label: string;
}

/**
 * Sentence-length items (activities, instructions) as a list of auto-growing
 * lines. Enter at the end of a line starts a new one; Backspace on an empty line
 * removes it and returns focus to the line above — it behaves like a document list.
 */
export function LineList({ values, onChange, placeholder, addLabel = "Add a line", readOnly, numbered, label }: LineListProps) {
  const refs = useRef<(HTMLTextAreaElement | null)[]>([]);
  const focusAt = useRef<number | null>(null);

  useEffect(() => {
    if (focusAt.current == null) return;
    const el = refs.current[focusAt.current];
    focusAt.current = null;
    if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
  });

  const set = (i: number, v: string) => onChange(values.map((x, j) => (j === i ? v : x)));
  const insertAfter = (i: number) => { focusAt.current = i + 1; onChange([...values.slice(0, i + 1), "", ...values.slice(i + 1)]); };
  const remove = (i: number) => { focusAt.current = Math.max(0, i - 1); onChange(values.filter((_, j) => j !== i)); };

  return (
    <div className={s.lines} role="list" aria-label={label}>
      {values.map((v, i) => (
        <div key={i} className={s.line} role="listitem">
          <span className={s.bullet} aria-hidden>{numbered ? `${i + 1}.` : "–"}</span>
          <SeamlessText ref={(el) => { refs.current[i] = el; }} value={v} readOnly={readOnly}
            placeholder={placeholder} aria-label={`${label} ${i + 1}`}
            onValue={(nv) => set(i, nv)}
            onKeyDown={(e) => {
              if (readOnly) return;
              const el = e.currentTarget;
              if (e.key === "Enter" && !e.shiftKey && el.selectionStart === el.value.length) { e.preventDefault(); insertAfter(i); }
              else if (e.key === "Backspace" && !el.value) { e.preventDefault(); remove(i); }
            }} />
          {!readOnly && (
            <button type="button" className={s.lineX} aria-label={`Remove line ${i + 1}`} onClick={() => remove(i)}>
              <X size={15} />
            </button>
          )}
        </div>
      ))}
      {!readOnly && (
        <button type="button" className={s.lineAdd} onClick={() => insertAfter(values.length - 1)}>
          <Plus size={14} /> {addLabel}
        </button>
      )}
    </div>
  );
}
