import nextCoreWebVitals from 'eslint-config-next/core-web-vitals';
import nextTypescript from 'eslint-config-next/typescript';
import prettier from 'eslint-config-prettier/flat';

const eslintConfig = [
    ...nextCoreWebVitals,
    ...nextTypescript,
    prettier,
    {
        rules: {
            '@typescript-eslint/no-unused-vars': 'off', // 不检查未使用的变量
            '@typescript-eslint/no-explicit-any': 'off', // 关闭 any 报错
            // 本项目静态导出并展示 data/blob 及任意来源图片，Next Image 不会进行服务端优化。
            '@next/next/no-img-element': 'off',
        },
    },
];

export default eslintConfig;
