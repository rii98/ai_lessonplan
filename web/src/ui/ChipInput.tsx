import { useRef, useState } from "react";
import { X } from "./icons";
import s from "./Lists.module.css";

export interface ChipInputProps {
  values: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  readOnly?: boolean;
  /** Accessible name for the group. */
  label: string;
}

/**
 * Short items as editable chips. Enter or comma adds; Backspace on an empty
 * field removes the last chip. Every chip stays editable in place.
 */
export function ChipInput({ values, onChange, placeholder = "Add…", readOnly, label }: ChipInputProps) {
  const [draft, setDraft] = useState("");
  const addRef = useRef<HTMLInputElement>(null);

  const commit = () => {
    const parts = draft.split(",").map((p) => p.trim()).filter(Boolean);
    if (parts.length) onChange([...values, ...parts]);
    setDraft("");
  };
  const set = (i: number, v: string) => onChange(values.map((x, j) => (j === i ? v : x)));
  const remove = (i: number) => onChange(values.filter((_, j) => j !== i));

  return (
    <div className={s.chips} role="group" aria-label={label}>
      {values.map((v, i) => (
        <span key={i} className={s.chip}>
          <input className={s.chipText} value={v} readOnly={readOnly} aria-label={`${label} ${i + 1}`}
            size={Math.max(2, v.length)}
            onChange={(e) => set(i, e.target.value)}
            onBlur={() => { if (!v.trim()) remove(i); }} />
          {!readOnly && (
            <button type="button" className={s.chipX} aria-label={`Remove ${v || "item"}`} onClick={() => remove(i)}>
              <X size={13} />
            </button>
          )}
        </span>
      ))}
      {!readOnly && (
        <input ref={addRef} className={s.chipAdd} value={draft} placeholder={placeholder} aria-label={`Add to ${label}`}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(); }
            else if (e.key === "Backspace" && !draft && values.length) { e.preventDefault(); remove(values.length - 1); }
          }} />
      )}
    </div>
  );
}
