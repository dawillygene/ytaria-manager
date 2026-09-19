import { AlertTriangle, CheckCircle2, Info, Loader2, X } from "lucide-react";
import { forwardRef, useEffect, useId, useRef, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode } from "react";
import { cx } from "../../lib/cx";

type Variant = "primary" | "secondary" | "danger" | "ghost";
const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent text-accentfg hover:brightness-110 disabled:opacity-50",
  secondary: "bg-surface2 text-fg border border-line hover:brightness-95 disabled:opacity-50",
  danger: "bg-dangersoft text-danger border border-danger/40 hover:brightness-95 disabled:opacity-50",
  ghost: "text-fg hover:bg-surface2 disabled:opacity-50",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  loading?: boolean;
  icon?: ReactNode;
}
/** Touch-friendly (44px min) button. `loading` disables it and announces busy state. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", loading, icon, children, className, disabled, type = "button", ...rest }, ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cx("inline-flex min-h-11 items-center justify-center gap-2 rounded-lg px-4 text-sm font-medium transition-colors disabled:cursor-not-allowed", VARIANTS[variant], className)}
      {...rest}
    >
      {loading ? <Loader2 className="size-4 animate-spin" aria-hidden /> : icon}
      {children}
    </button>
  );
});

export interface TextFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  error?: string | null;
  hint?: string;
}
export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField({ label, error, hint, id, className, ...rest }, ref) {
  const auto = useId();
  const fieldId = id ?? auto;
  const describedBy = [error ? `${fieldId}-err` : null, hint ? `${fieldId}-hint` : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="space-y-1.5">
      <label htmlFor={fieldId} className="block text-sm font-medium">{label}</label>
      <input
        ref={ref}
        id={fieldId}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className={cx("min-h-11 w-full rounded-lg border bg-surface px-3 text-base text-fg placeholder:text-muted", error ? "border-danger" : "border-line", className)}
        {...rest}
      />
      {hint && !error && <p id={`${fieldId}-hint`} className="text-sm text-muted">{hint}</p>}
      {error && <p id={`${fieldId}-err`} className="text-sm text-danger">{error}</p>}
    </div>
  );
});

export function Card({ children, className, as: Tag = "section", ...rest }: { children: ReactNode; className?: string; as?: "section" | "div" | "li" | "article"; "aria-label"?: string; "aria-labelledby"?: string }) {
  return <Tag className={cx("rounded-xl border border-line bg-surface p-4 shadow-sm", className)} {...rest}>{children}</Tag>;
}

type Tone = "neutral" | "accent" | "ok" | "warn" | "danger" | "info";
const TONES: Record<Tone, string> = {
  neutral: "bg-surface2 text-muted", accent: "bg-accentsoft text-accent", ok: "bg-oksoft text-ok",
  warn: "bg-warnsoft text-warn", danger: "bg-dangersoft text-danger", info: "bg-infosoft text-info",
};
export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={cx("inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold", TONES[tone])}>{children}</span>;
}

export function ProgressBar({ value, label, indeterminate }: { value: number; label: string; indeterminate?: boolean }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : Math.round(pct)}
      className="h-2 w-full overflow-hidden rounded-full bg-surface2"
    >
      <div className={cx("h-full rounded-full bg-accent transition-[width] duration-300", indeterminate && "w-1/3 animate-pulse")} style={indeterminate ? undefined : { width: `${pct}%` }} />
    </div>
  );
}

const ALERT_STYLE = {
  error: { cls: "border-danger/40 bg-dangersoft text-danger", Icon: AlertTriangle, role: "alert" as const },
  warning: { cls: "border-warn/40 bg-warnsoft text-warn", Icon: AlertTriangle, role: "status" as const },
  info: { cls: "border-info/40 bg-infosoft text-info", Icon: Info, role: "status" as const },
  success: { cls: "border-ok/40 bg-oksoft text-ok", Icon: CheckCircle2, role: "status" as const },
};
export function Alert({ kind = "info", children, action }: { kind?: keyof typeof ALERT_STYLE; children: ReactNode; action?: ReactNode }) {
  const { cls, Icon, role } = ALERT_STYLE[kind];
  return (
    <div role={role} className={cx("flex items-start gap-3 rounded-lg border p-3 text-sm", cls)}>
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">{children}</div>
      {action}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center justify-center gap-2 py-8 text-muted">
      <Loader2 className="size-5 animate-spin" aria-hidden /> <span>{label}…</span>
    </div>
  );
}

export function EmptyState({ title, children, icon }: { title: string; children?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-line px-6 py-10 text-center">
      {icon && <div className="text-muted" aria-hidden>{icon}</div>}
      <p className="font-medium">{title}</p>
      {children && <p className="max-w-sm text-sm text-muted">{children}</p>}
    </div>
  );
}

/** Modal built on the native <dialog> element: focus trap, Esc to close, inert background. */
export function ConfirmDialog({
  open, title, children, confirmLabel, onConfirm, onCancel, danger, busy,
}: { open: boolean; title: string; children: ReactNode; confirmLabel: string; onConfirm: () => void; onCancel: () => void; danger?: boolean; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(e) => { e.preventDefault(); onCancel(); }}
      className="m-auto w-[min(92vw,26rem)] rounded-xl border border-line bg-surface p-0 text-fg shadow-xl"
    >
      <div className="space-y-3 p-5">
        <div className="flex items-start justify-between gap-3">
          <h2 id={titleId} className="text-lg font-semibold">{title}</h2>
          <button type="button" aria-label="Close" onClick={onCancel} className="grid size-9 place-items-center rounded-lg hover:bg-surface2"><X className="size-4" /></button>
        </div>
        <div className="text-sm text-muted">{children}</div>
        <div className="flex justify-end gap-2 pt-2">
          <Button onClick={onCancel} disabled={busy}>Keep it</Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} loading={busy}>{confirmLabel}</Button>
        </div>
      </div>
    </dialog>
  );
}
