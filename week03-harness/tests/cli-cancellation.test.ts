import { describe, expect, it } from "vitest";
import { installSigintCancellation } from "../src/agent/cli-cancellation.js";

class FakeProcess {
  #listeners = new Map<string, (...args: never[]) => void>();

  once(event: string, listener: (...args: never[]) => void): this {
    this.#listeners.set(event, listener);
    return this;
  }

  removeListener(event: string, listener: (...args: never[]) => void): this {
    if (this.#listeners.get(event) === listener) {
      this.#listeners.delete(event);
    }
    return this;
  }

  emitSigint(): void {
    const listener = this.#listeners.get("SIGINT");
    this.#listeners.delete("SIGINT");
    listener?.();
  }

  get hasSigintListener(): boolean {
    return this.#listeners.has("SIGINT");
  }
}

describe("CLI Ctrl+C cancellation", () => {
  it("converts the first SIGINT into the injected Run AbortSignal", () => {
    const controller = new AbortController();
    const processLike = new FakeProcess();
    installSigintCancellation(controller, processLike as unknown as typeof process);

    processLike.emitSigint();

    expect(controller.signal.aborted).toBe(true);
    expect(processLike.hasSigintListener).toBe(false);
  });

  it("removes an unused SIGINT listener when the Run completes normally", () => {
    const controller = new AbortController();
    const processLike = new FakeProcess();
    const remove = installSigintCancellation(controller, processLike as unknown as typeof process);

    expect(processLike.hasSigintListener).toBe(true);
    remove();

    expect(processLike.hasSigintListener).toBe(false);
    expect(controller.signal.aborted).toBe(false);
  });
});
