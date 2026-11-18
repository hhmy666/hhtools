import {RANDOM_ID_A, RANDOM_ID_N} from './logic.js';

const COLORS = [
  '#ffbf3f', '#55c7ff', '#62dc8c', '#ff7188', '#b799ff', '#ff974d', '#66a0ff',
  '#ff70c7', '#36d5d8', '#d99b66', '#e6db4f', '#9d7cff', '#4ed06d', '#ff625c',
]         ;

export function randomMarkerIndex(tileId       )             {
  return tileId<=RANDOM_ID_A&&tileId>=RANDOM_ID_N?RANDOM_ID_A-tileId:null;
}
export function randomMarkerColor(tileId       )             {
  const index=randomMarkerIndex(tileId);return index===null?null:COLORS[index] ;
}
