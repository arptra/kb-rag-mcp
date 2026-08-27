declare module "node:fs" {
  export function existsSync(path: string): boolean;
  export function readFileSync(path: string): Uint8Array;
}

declare module "node:path" {
  export function resolve(...paths: string[]): string;
}

declare module "node:url" {
  export function fileURLToPath(url: URL | string): string;
}

declare const process: {
  env: Record<string, string | undefined>;
};
