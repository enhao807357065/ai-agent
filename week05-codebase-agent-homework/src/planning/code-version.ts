/** 单个运行内的可信代码版本。仅在受控写入成功后推进。 */
export class CodeVersionTracker {
  #revision: number;

  constructor(revision = 0) {
    if (!Number.isInteger(revision) || revision < 0) throw new Error("CODE_REVISION_INVALID");
    this.#revision = revision;
  }

  revision(): number {
    return this.#revision;
  }

  current(): string {
    return `run-code-${this.#revision}`;
  }

  advanceAfterCodeChange(): string {
    this.#revision += 1;
    return this.current();
  }
}
