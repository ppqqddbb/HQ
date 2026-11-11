/* 全局共享状态 */
export const state = {
  dataset: null,
  overview: null,
  tasks: {},
  lastResults: {},
};

export function setResult(key, value) {
  state.lastResults[key] = value;
}

export function getResult(key) {
  return state.lastResults[key];
}
