import { clsx } from "clsx";
import {
  forwardRef, useCallback, useId, useLayoutEffect, useRef,
  type InputHTMLAttributes, type ReactElement, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes,
} from "react";
import { cloneElement, isValidElement } from "react";
import s from "./Field.module.css";

export interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  optional?: boolean;
  className?: string;
  /** The single control; it receives the id/aria wiring. */
  children: ReactElement;
}

/** Label + control + hint/error, wired for screen readers. */
export function Field({ label, hint, error, optional, className, children }: FieldProps) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errId = error ? `${id}-err` : undefined;
  const control = isValidElement(children)
    ? cloneElement(children as ReactElement<Record<string, unknown>>, {
        id, "aria-describedby": [hintId, errId].filter(Boolean).join(" ") || undefined,
        "aria-invalid": error ? true : undefined,
      })
    : children;
  return (
    <div className={clsx(s.field, className)}>
      <label htmlFor={id} className={s.label}>
        {label}{optional && <span className={s.optional}>optional</span>}
      </label>
      {control}
      {hint && !error && <span id={hintId} className={s.hint}>{hint}</span>}
      {error && <span id={errId} className={s.error} role="alert">{error}</span>}
    </div>
  );
}

export const FieldGrid = ({ children, className }: { children: ReactNode; className?: string }) =>
  <div className={clsx(s.grid, className)}>{children}</div>;

export const TextInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function TextInput({ className, ...rest }, ref) {
    return <input ref={ref} className={clsx(s.control, className)} {...rest} />;
  });

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, ...rest }, ref) {
    return <select ref={ref} className={clsx(s.control, className)} {...rest} />;
  });

/** Grow a textarea to fit its content (no inner scrollbar, no clipped text). */
function useAutoGrow(value: unknown) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const fit = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  }, []);
  useLayoutEffect(fit, [value, fit]);
  return { ref, fit };
}

function mergeRefs<T>(...refs: (React.Ref<T> | undefined)[]) {
  return (el: T) => refs.forEach((r) => {
    if (typeof r === "function") r(el);
    else if (r) (r as React.MutableRefObject<T>).current = el;
  });
}

export interface TextAreaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  autoGrow?: boolean;
}

export const TextArea = forwardRef<HTMLTextAreaElement, TextAreaProps>(
  function TextArea({ className, autoGrow = false, rows = 3, ...rest }, ref) {
    const g = useAutoGrow(rest.value);
    return <textarea ref={mergeRefs(ref, g.ref)} rows={rows}
      className={clsx(s.control, autoGrow && s.autogrow, className)} {...rest} />;
  });

export interface SeamlessTextProps extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "onChange" | "value"> {
  value: string;
  onValue: (v: string) => void;
  /** Single line: Enter doesn't insert a newline. */
  singleLine?: boolean;
  invalid?: boolean;
}

/**
 * Text that looks like part of the document and becomes an input on hover/focus —
 * the editor's main control. Always an auto-growing textarea so long text wraps.
 */
export const SeamlessText = forwardRef<HTMLTextAreaElement, SeamlessTextProps>(
  function SeamlessText({ value, onValue, singleLine, invalid, className, onKeyDown, ...rest }, ref) {
    const g = useAutoGrow(value);
    return (
      <textarea ref={mergeRefs(ref, g.ref)} rows={1} value={value} spellCheck
        aria-invalid={invalid || undefined}
        className={clsx(s.seamless, className)}
        onChange={(e) => onValue(singleLine ? e.target.value.replace(/\n/g, " ") : e.target.value)}
        onKeyDown={(e) => {
          if (singleLine && e.key === "Enter") e.preventDefault();
          onKeyDown?.(e);
        }}
        {...rest} />
    );
  });

export interface NumberInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "type"> {
  value: number | null | undefined;
  onValue: (v: number | null) => void;
}

export const NumberInput = forwardRef<HTMLInputElement, NumberInputProps>(
  function NumberInput({ value, onValue, className, ...rest }, ref) {
    return (
      <input ref={ref} type="number" inputMode="numeric" className={clsx(s.control, className)}
        value={value ?? ""} onChange={(e) => onValue(e.target.value === "" ? null : Number(e.target.value))} {...rest} />
    );
  });
