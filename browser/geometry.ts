export type Rect = { x: number; y: number; width: number; height: number };
export type Box = [number, number, number, number];

export function imageBox(rect: Rect, dpr: number, crop: [number, number] = [0, 0]): Box {
  if (![...Object.values(rect), dpr, ...crop].every(Number.isFinite) ||
      rect.width <= 0 || rect.height <= 0 || dpr <= 0) throw new Error('invalid geometry');
  return [rect.x * dpr - crop[0], rect.y * dpr - crop[1],
    (rect.x + rect.width) * dpr - crop[0], (rect.y + rect.height) * dpr - crop[1]];
}
