// @types/react-syntax-highlighter@15 未覆盖 v16 的子路径模块(入口/语言/样式单文件),
// 且 dashboard tsconfig types 白名单会跳过 @types 自动载入;仅 RegisterLanguage 取用,类型宽松即可
declare module 'react-syntax-highlighter/dist/esm/prism';
declare module 'react-syntax-highlighter/dist/esm/prism-light';
declare module 'react-syntax-highlighter/dist/esm/languages/prism/*';
declare module 'react-syntax-highlighter/dist/esm/styles/prism/*';
