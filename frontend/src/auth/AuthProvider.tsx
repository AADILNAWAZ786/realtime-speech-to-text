import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  use,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { useApi } from "../api/ApiProvider";
import { ApiError } from "../api/client";
import type { RegisterRequest, User } from "../api/types";
import { useToast } from "../components/ui/Toast";
import { tokenStorage } from "./tokenStorage";

export type AuthStatus = "loading" | "authenticated" | "anonymous" | "error";

export interface AuthContextValue {
  status: AuthStatus;
  user: User | null;
  /** Set when restoring the session failed for a reason other than an invalid token. */
  restoreError: ApiError | null;
  login: (email: string, password: string) => Promise<void>;
  register: (data: RegisterRequest) => Promise<void>;
  logout: () => void;
  retryRestore: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const { endpoints, onUnauthorized } = useApi();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [user, setUser] = useState<User | null>(null);
  const [status, setStatus] = useState<AuthStatus>(() =>
    tokenStorage.get() ? "loading" : "anonymous",
  );
  const [restoreError, setRestoreError] = useState<ApiError | null>(null);
  const [restoreAttempt, setRestoreAttempt] = useState(0);

  const signOut = useCallback(() => {
    tokenStorage.clear();
    queryClient.clear(); // never show one user's cached data to the next
    setUser(null);
    setStatus("anonymous");
  }, [queryClient]);

  // Restore the session on page load / refresh from the stored token.
  useEffect(() => {
    if (!tokenStorage.get()) return;
    const controller = new AbortController();
    endpoints.auth
      .me(controller.signal)
      .then((me) => {
        setUser(me);
        setRestoreError(null);
        setStatus("authenticated");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.isUnauthorized) {
          signOut(); // expired or revoked token
        } else {
          setRestoreError(error instanceof ApiError ? error : null);
          setStatus("error"); // server unreachable: keep the token, offer a retry
        }
      });
    return () => controller.abort();
  }, [endpoints, signOut, restoreAttempt]);

  // Any authenticated request rejected with 401 ends the session.
  useEffect(
    () =>
      onUnauthorized(() => {
        if (!tokenStorage.get() && status === "anonymous") return;
        signOut();
        toast.show({ kind: "info", title: "Your session has ended. Please sign in again." });
      }),
    [onUnauthorized, signOut, status, toast],
  );

  const login = useCallback(
    async (email: string, password: string) => {
      const token = await endpoints.auth.login({ email, password });
      tokenStorage.set(token.access_token, token.expires_in);
      try {
        const me = await endpoints.auth.me();
        setUser(me);
        setStatus("authenticated");
      } catch (error) {
        tokenStorage.clear();
        throw error;
      }
    },
    [endpoints],
  );

  const register = useCallback(
    async (data: RegisterRequest) => {
      await endpoints.auth.register(data);
      await login(data.email, data.password);
    },
    [endpoints, login],
  );

  const logout = useCallback(() => {
    signOut();
    toast.show({ kind: "success", title: "Signed out" });
  }, [signOut, toast]);

  const retryRestore = useCallback(() => {
    setStatus("loading");
    setRestoreAttempt((n) => n + 1);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, restoreError, login, register, logout, retryRestore }),
    [status, user, restoreError, login, register, logout, retryRestore],
  );
  return <AuthContext value={value}>{children}</AuthContext>;
}

export function useAuth(): AuthContextValue {
  const value = use(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
