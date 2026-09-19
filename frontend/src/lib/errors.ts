import type { ApiErrorBody } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public fields?: ApiErrorBody["fields"]) {
    super(message);
    this.name = "ApiError";
  }
}

/** The request never reached the server (offline, DNS, TLS, server down). Never implies "signed out". */
export class NetworkError extends Error {
  constructor() {
    super("Can't reach the server. Check your connection and try again.");
    this.name = "NetworkError";
  }
}
