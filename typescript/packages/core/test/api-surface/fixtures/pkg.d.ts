// A fixture package that satisfies fixtures/api.json. surface.test.ts edits copies of it.

export declare function one(options: {
  readonly a: string;
  readonly b?: number;
}): void;

export declare function two(options: { readonly a: string }): void;
export declare function two(options: {
  readonly a?: string;
  readonly c: number;
}): void;

export declare function three(options: { readonly a: string }): void;
export declare function three(options: {
  readonly a: string;
  readonly b: number;
}): void;
export declare function three(options: {
  readonly a: string;
  readonly c?: number;
}): void;

export declare function four(options: { readonly a: string }): void;
export declare function four(options: { readonly a: "x" }): void;
export declare function four(options: { readonly a: "y" }): void;
export declare function four(options: {
  readonly a: string;
  readonly d: number;
}): void;

export type Model = {
  readonly info: string;
  readonly send: () => void;
  readonly lookup?: () => void;
};
