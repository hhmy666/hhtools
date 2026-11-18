/**
 * 撤销栈，存整份快照。配置表 / 关卡文件只有几 KB，不值得为增量撤销付复杂度——
 * 和参照的 tilematch-layout-editor 一个思路。T 由调用方定（Tile 编辑器里是整份 Catalog）。
 */
export class History    {
          undoStack      = [];
          redoStack      = [];
                   limit        ;
  // 不用构造参数属性：node --test 的类型擦除不支持那种写法
  constructor(limit = 200) { this.limit = limit; }

  /** 改动前调用，记下改动前的样子 */
  push(before   )       {
    this.undoStack.push(before);
    if (this.undoStack.length > this.limit) this.undoStack.shift();
    this.redoStack.length = 0;
  }

  undo(current   )           {
    const prev = this.undoStack.pop();
    if (prev === undefined) return null;
    this.redoStack.push(current);
    return prev;
  }

  redo(current   )           {
    const next = this.redoStack.pop();
    if (next === undefined) return null;
    this.undoStack.push(current);
    return next;
  }

  clear()       {
    this.undoStack.length = 0;
    this.redoStack.length = 0;
  }

  get canUndo()          { return this.undoStack.length > 0; }
  get canRedo()          { return this.redoStack.length > 0; }
}
