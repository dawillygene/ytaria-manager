import { Download } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Alert, Button, Card, TextField } from "../../components/ui";
import { ApiError } from "../../lib/errors";
import { useSession } from "../../lib/hooks";
import { useConfig, errorMessage } from "../downloads/queries";

export function AuthScreen() {
  const session = useSession();
  const config = useConfig();
  const registration = config.data?.registration ?? "open";
  const minLen = config.data?.password_min_length ?? 10;
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string }>({});

  function validate(): boolean {
    const next: typeof fieldErrors = {};
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email.trim())) next.email = "Enter a valid email address.";
    if (mode === "register" && password.length < minLen) next.password = `Use at least ${minLen} characters.`;
    if (mode === "login" && !password) next.password = "Enter your password.";
    setFieldErrors(next);
    return Object.keys(next).length === 0;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (busy || !validate()) return;
    setBusy(true);
    try {
      const body: Record<string, string> = { email: email.trim(), password };
      if (mode === "register" && invite) body.invite_code = invite;
      await session.authenticate(mode === "login" ? "/api/auth/login" : "/api/auth/register", body);
    } catch (err) {
      setError(err instanceof ApiError && err.code === "rate_limited" ? "Too many attempts. Wait a few minutes and try again." : errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="safe-top safe-bottom safe-x grid min-h-dvh place-items-center py-8">
      <div className="w-full max-w-sm space-y-5">
        <header className="flex flex-col items-center gap-2 text-center">
          <span className="grid size-12 place-items-center rounded-xl bg-accent text-accentfg"><Download aria-hidden /></span>
          <h1 className="text-2xl font-semibold">ytaria-manager</h1>
          <p className="text-sm text-muted">Queue a download on the server, then save it to this device.</p>
        </header>
        <Card>
          <form onSubmit={submit} noValidate className="space-y-4" aria-label={mode === "login" ? "Sign in" : "Create account"}>
            <h2 className="text-lg font-semibold">{mode === "login" ? "Sign in" : "Create your account"}</h2>
            {error && <Alert kind="error">{error}</Alert>}
            <TextField label="Email" type="email" autoComplete="email" inputMode="email" value={email} onChange={(e) => setEmail(e.target.value)} error={fieldErrors.email} />
            <TextField
              label="Password" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === "login" ? "current-password" : "new-password"} error={fieldErrors.password}
              hint={mode === "register" ? `At least ${minLen} characters.` : undefined}
            />
            {mode === "register" && registration === "invite" && (
              <TextField label="Invite code" value={invite} onChange={(e) => setInvite(e.target.value)} autoComplete="off" />
            )}
            <Button type="submit" variant="primary" className="w-full" loading={busy}>{mode === "login" ? "Sign in" : "Create account"}</Button>
          </form>
        </Card>
        {registration !== "closed" ? (
          <p className="text-center text-sm text-muted">
            {mode === "login" ? "New here? " : "Already have an account? "}
            <button type="button" className="min-h-11 font-medium text-accent underline" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(null); setFieldErrors({}); }}>
              {mode === "login" ? "Create an account" : "Sign in"}
            </button>
          </p>
        ) : (
          <p className="text-center text-sm text-muted">Registration is closed on this server.</p>
        )}
      </div>
    </main>
  );
}
