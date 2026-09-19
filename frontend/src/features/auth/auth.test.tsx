import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { session } from "../../lib/session";
import { mockFetch, renderWithClient } from "../../test/utils";
import { AuthScreen } from "./AuthScreen";

const user = { id: "u1", email: "a@b.co", display_name: "a" };
beforeEach(() => session.resetForTests());

describe("AuthScreen", () => {
  it("validates before calling the server", async () => {
    const { calls } = mockFetch({ "GET /api/config": () => ({ body: { app: "x", registration: "open", password_min_length: 10 } }) });
    const u = userEvent.setup();
    renderWithClient(<AuthScreen />);
    await u.type(screen.getByLabelText("Email"), "nope");
    await u.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/api/auth/login")).toBe(false);
  });

  it("signs in and shows the server's error on bad credentials", async () => {
    let ok = false;
    mockFetch({
      "GET /api/config": () => ({ body: { app: "x", registration: "open", password_min_length: 10 } }),
      "POST /api/auth/login": () => ok ? { body: { user, csrf_token: "c" } } : { status: 401, body: { error: { code: "invalid_credentials", message: "Incorrect email or password." } } },
    });
    const u = userEvent.setup();
    renderWithClient(<AuthScreen />);
    await u.type(screen.getByLabelText("Email"), "a@b.co");
    await u.type(screen.getByLabelText("Password"), "wrong-password");
    await u.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password.");
    ok = true;
    await u.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(session.state).toBe("signed-in"));
  });

  it("registers with the minimum password length and reports rate limiting", async () => {
    mockFetch({
      "GET /api/config": () => ({ body: { app: "x", registration: "open", password_min_length: 10 } }),
      "POST /api/auth/register": () => ({ status: 429, body: { error: { code: "rate_limited", message: "Too many requests." } } }),
    });
    const u = userEvent.setup();
    renderWithClient(<AuthScreen />);
    await u.click(screen.getByRole("button", { name: "Create an account" }));
    await u.type(screen.getByLabelText("Email"), "a@b.co");
    await u.type(screen.getByLabelText("Password"), "short");
    await u.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByText("Use at least 10 characters.")).toBeInTheDocument();
    await u.clear(screen.getByLabelText("Password"));
    await u.type(screen.getByLabelText("Password"), "long enough password");
    await u.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts");
  });

  it("hides registration when closed and asks for an invite code when required", async () => {
    mockFetch({ "GET /api/config": () => ({ body: { app: "x", registration: "closed", password_min_length: 10 } }) });
    renderWithClient(<AuthScreen />);
    expect(await screen.findByText("Registration is closed on this server.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create an account" })).toBeNull();
  });

  it("invite mode shows the code field", async () => {
    mockFetch({ "GET /api/config": () => ({ body: { app: "x", registration: "invite", password_min_length: 10 } }) });
    const u = userEvent.setup();
    renderWithClient(<AuthScreen />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Create an account" })).toBeEnabled());
    await u.click(screen.getByRole("button", { name: "Create an account" }));
    expect(await screen.findByLabelText("Invite code")).toBeInTheDocument();
  });
});
