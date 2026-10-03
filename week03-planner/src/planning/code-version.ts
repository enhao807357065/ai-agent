/**
 * 单 Run 的服务端代码版本源。
 *
 * 模型永远不能提交 codeVersion。每次受控 modify_code 成功后由 Runtime 推进版本，
 * PlanStore 在写入 change/test Evidence 时读取当前值，从而使修改前的测试证据不能
 * 被用于验收修改后的代码。
 */
export class CodeVersionTracker {
  #revision = 0;

  current(): string {
    return `run-code-${this.#revision}`;
  }

  advanceAfterCodeChange(): string {
    this.#revision += 1;
    return this.current();
  }
}
