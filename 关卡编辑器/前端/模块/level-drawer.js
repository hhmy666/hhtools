


















export const initialDrawerState              = {
  open: false,
  status: 'idle',
  rows: [],
  selectedLevelId: '',
  error: '',
};

export function reduceDrawer(state             , action              )              {
  if (action.type === 'toggle') {
    const open = !state.open;
    return { ...state, open, status: open && state.status === 'idle' ? 'loading' : state.status };
  }
  if (action.type === 'close') return { ...state, open: false };
  if (action.type === 'loaded') return { ...state, status: 'ready', rows: action.rows, error: '' };
  if (action.type === 'failed') return { ...state, status: 'error', rows: [], error: action.error };
  if (!action.loadable) return state;
  return { ...state, selectedLevelId: action.levelId };
}
