#!/bin/bash
# Setup React + webpack5 coding benchmark project inside Docker image
# Generates a project with 40 React components for memory-intensive webpack builds
#
# Usage: Called from Dockerfile.coding during docker build
# Result: /opt/coding-bench/ with pre-installed node_modules and initial build

set -e

PROJECT_DIR="/opt/coding-bench"
COMPONENT_COUNT=40

echo "=== Setting up coding benchmark project ==="
echo "  Project dir: ${PROJECT_DIR}"
echo "  Components:  ${COMPONENT_COUNT}"

mkdir -p "${PROJECT_DIR}"
cd "${PROJECT_DIR}"

# -------------------------------------------------------
# 1. Create package.json
# -------------------------------------------------------
cat > package.json << 'PKGJSON'
{
  "name": "coding-bench",
  "version": "1.0.0",
  "description": "E2B benchmark project for AI coding agent memory stress testing",
  "scripts": {
    "build": "webpack --mode production --config webpack.config.js",
    "test": "jest --no-cache --ci --forceExit",
    "clean": "rm -rf dist"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "typescript": "^5.5.4",
    "webpack": "^5.94.0",
    "webpack-cli": "^6.0.1",
    "ts-loader": "^9.5.1",
    "html-webpack-plugin": "^5.6.3",
    "mini-css-extract-plugin": "^2.9.2",
    "css-loader": "^7.1.2",
    "style-loader": "^4.0.0",
    "@types/react": "^18.3.5",
    "@types/react-dom": "^18.3.0",
    "jest": "^29.7.0",
    "ts-jest": "^29.2.5",
    "@testing-library/react": "^16.2.0",
    "@testing-library/jest-dom": "^6.6.3",
    "jest-environment-jsdom": "^29.7.0"
  }
}
PKGJSON

echo "[1/8] package.json created"

# -------------------------------------------------------
# 2. Create webpack.config.js (webpack5 production)
# -------------------------------------------------------
cat > webpack.config.js << 'WEBPACK'
const path = require('path');
const HtmlWebpackPlugin = require('html-webpack-plugin');
const MiniCssExtractPlugin = require('mini-css-extract-plugin');

module.exports = {
  mode: 'production',
  entry: './src/index.tsx',
  output: {
    path: path.resolve(__dirname, 'dist'),
    filename: '[name].[contenthash:8].js',
    clean: true,
  },
  resolve: {
    extensions: ['.tsx', '.ts', '.js', '.jsx'],
  },
  module: {
    rules: [
      {
        test: /\.tsx?$/,
        use: 'ts-loader',
        exclude: /node_modules/,
      },
      {
        test: /\.css$/,
        use: [MiniCssExtractPlugin.loader, 'css-loader'],
      },
    ],
  },
  plugins: [
    new HtmlWebpackPlugin({
      template: './public/index.html',
      minify: { collapseWhitespace: true, removeComments: true },
    }),
    new MiniCssExtractPlugin({
      filename: '[name].[contenthash:8].css',
    }),
  ],
  optimization: {
    splitChunks: {
      chunks: 'all',
      minSize: 20000,
      maxSize: 244000,
      cacheGroups: {
        vendors: {
          test: /[\\/]node_modules[\\/]/,
          name: 'vendors',
          chunks: 'all',
        },
      },
    },
    runtimeChunk: 'single',
  },
  performance: {
    hints: false,
    maxEntrypointSize: 512000,
    maxAssetSize: 512000,
  },
};
WEBPACK

echo "[2/8] webpack.config.js created"

# -------------------------------------------------------
# 3. Create tsconfig.json
# -------------------------------------------------------
cat > tsconfig.json << 'TSCONFIG'
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "lib": ["DOM", "DOM.Iterable", "ESNext"],
    "jsx": "react-jsx",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "moduleResolution": "node",
    "declaration": false,
    "outDir": "./dist",
    "rootDir": "./src",
    "sourceMap": false
  },
  "include": ["src"],
  "exclude": ["node_modules", "dist", "src/__tests__"]
}
TSCONFIG

echo "[3/8] tsconfig.json created"

# -------------------------------------------------------
# 4. Create jest.config.js
# -------------------------------------------------------
cat > jest.config.js << 'JEST'
module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'jsdom',
  roots: ['<rootDir>/src/__tests__'],
  testMatch: ['**/*.test.tsx'],
  moduleFileExtensions: ['tsx', 'ts', 'js', 'jsx'],
  transform: {
    '^.+\\.tsx?$': 'ts-jest',
  },
  setupFilesAfterSetup: [],
};
JEST

echo "[4/8] jest.config.js created"

# -------------------------------------------------------
# 5. Create source directory structure
# -------------------------------------------------------
mkdir -p src/components src/styles src/utils src/__tests__ public

# HTML template
cat > public/index.html << 'HTML'
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Coding Bench</title>
</head>
<body>
  <div id="root"></div>
</body>
</html>
HTML

# bench-marker.ts — shared constant modified each round
cat > src/bench-marker.ts << 'BENCH'
// This file is modified each benchmark round to trigger webpack rebuild
// sed pattern: s/export const BENCH_ROUND = .*/export const BENCH_ROUND = ${round_id};/
export const BENCH_ROUND = 0;
export const BENCH_ID = "baseline";
BENCH

echo "[5a/8] bench-marker.ts created"

# App.css — main stylesheet
cat > src/styles/App.css << 'CSS'
.app-container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 20px;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}
.app-header {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  padding: 16px 24px;
  border-radius: 8px;
  margin-bottom: 20px;
}
.app-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
  padding: 16px;
}
.component-card {
  background: #ffffff;
  border: 1px solid #e0e0e0;
  border-radius: 8px;
  padding: 16px;
  box-shadow: 0 2px 4px rgba(0,0,0,0.1);
  transition: transform 0.2s, box-shadow 0.2s;
}
.component-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 8px rgba(0,0,0,0.15);
}
.component-title {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 8px;
  color: #333;
}
.component-body {
  font-size: 14px;
  color: #666;
  line-height: 1.6;
}
.component-footer {
  margin-top: 12px;
  font-size: 12px;
  color: #999;
}
CSS

echo "[5b/8] App.css created"

# Component CSS (shared)
cat > src/styles/components.css << 'COMPCSS'
.btn {
  display: inline-block;
  padding: 8px 16px;
  border-radius: 4px;
  border: none;
  cursor: pointer;
  font-size: 14px;
  transition: background 0.3s;
}
.btn-primary { background: #4CAF50; color: white; }
.btn-secondary { background: #2196F3; color: white; }
.btn-danger { background: #f44336; color: white; }
.list-items { list-style: none; padding: 0; }
.list-items li {
  padding: 8px 0;
  border-bottom: 1px solid #eee;
}
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  background: #e3f2fd;
  color: #1976d2;
}
COMPCSS

echo "[5c/8] components.css created"

# Utils
cat > src/utils/helpers.ts << 'HELPERS'
export function formatNumber(n: number): string {
  return n.toLocaleString();
}

export function generateId(): string {
  return Math.random().toString(36).substring(2, 9);
}

export function truncate(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str;
  return str.substring(0, maxLen) + '...';
}

export const ITEMS = ['Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta', 'Eta', 'Theta'];
HELPERS

cat > src/utils/api.ts << 'API'
export interface ApiResponse<T> {
  data: T;
  status: number;
  message: string;
}

export async function fetchData<T>(url: string): Promise<ApiResponse<T>> {
  return {
    data: {} as T,
    status: 200,
    message: 'OK',
  };
}

export const API_ENDPOINTS = {
  users: '/api/users',
  posts: '/api/posts',
  comments: '/api/comments',
  settings: '/api/settings',
};
API

cat > src/utils/format.ts << 'FORMAT'
export function formatDate(date: Date): string {
  return date.toISOString().split('T')[0];
}

export function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(amount);
}

export function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}
FORMAT

echo "[5d/8] utility modules created"

# -------------------------------------------------------
# 6. Generate React component files (40 components)
# -------------------------------------------------------
COMPONENT_NAMES=(
  "Dashboard" "Header" "Footer" "Sidebar" "Navigation"
  "LoginForm" "RegisterForm" "UserProfile" "SettingsPanel" "NotificationBar"
  "DataTable" "ChartView" "PieChart" "LineGraph" "BarChart"
  "SearchBar" "FilterPanel" "SortControl" "Pagination" "InfiniteList"
  "ModalDialog" "ConfirmDialog" "AlertDialog" "TooltipView" "PopoverMenu"
  "Accordion" "TabView" "Breadcrumb" "StepperControl" "ProgressBar"
  "FileUploader" "ImageGallery" "VideoPlayer" "AudioPlayer" "CarouselView"
  "CodeEditor" "MarkdownView" "DiffViewer" "TerminalView" "TaskBoard"
)

# Generate component import list for App.tsx
IMPORT_LIST=""
COMPONENT_RENDER_LIST=""
TEST_IMPORT_LIST=""

for i in $(seq 0 $((${COMPONENT_COUNT} - 1))); do
    NAME="${COMPONENT_NAMES[$i]}"
    cat > "src/components/${NAME}.tsx" << COMPFILE
import React, { useState } from 'react';
import { BENCH_ROUND, BENCH_ID } from '../bench-marker';
import { formatNumber, ITEMS } from '../utils/helpers';
import '../styles/components.css';

export const ${NAME}: React.FC = () => {
  const [count, setCount] = useState(0);
  const benchRound = BENCH_ROUND;
  const benchId = BENCH_ID;

  return (
    <div data-testid="${NAME}" className="component-card">
      <h2 className="component-title">${NAME} (Round {benchRound})</h2>
      <div className="component-body">
        <p>Benchmark component #${i} — ID: {benchId}</p>
        <p>Counter: {formatNumber(count)}</p>
        <ul className="list-items">
          {ITEMS.slice(0, 4).map((item, idx) => (
            <li key={idx}>{item} - ${NAME}</li>
          ))}
        </ul>
      </div>
      <div className="component-footer">
        <button
          className="btn btn-primary"
          onClick={() => setCount(count + 1)}
        >
          Increment
        </button>
        <span className="badge">v${i}</span>
      </div>
    </div>
  );
};
COMPFILE

    IMPORT_LIST="${IMPORT_LIST}import { ${NAME} } from './components/${NAME}';
"
    COMPONENT_RENDER_LIST="${COMPONENT_RENDER_LIST}          <${NAME} />
"

    # Generate test file for every 4th component (10 test files)
    if [ $((i % 4)) -eq 0 ]; then
        cat > "src/__tests__/${NAME}.test.tsx" << TESTFILE
import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { ${NAME} } from '../components/${NAME}';

describe('${NAME}', () => {
  test('renders component title', () => {
    render(<${NAME} />);
    const titleElement = screen.getByTestId('${NAME}');
    expect(titleElement).toBeInTheDocument();
  });

  test('displays component name in heading', () => {
    render(<${NAME} />);
    const heading = screen.getByRole('heading');
    expect(heading.textContent).toContain('${NAME}');
  });

  test('has increment button', () => {
    render(<${NAME} />);
    const button = screen.getByRole('button', { name: /increment/i });
    expect(button).toBeInTheDocument();
  });
});
TESTFILE
        TEST_IMPORT_LIST="${TEST_IMPORT_LIST}import { ${NAME} } from '../components/${NAME}';
"
    fi
done

echo "[6/8] ${COMPONENT_COUNT} component files generated"

# -------------------------------------------------------
# 7. Create App.tsx and index.tsx
# -------------------------------------------------------
cat > src/App.tsx << APPEOF
import React from 'react';
import { BENCH_ROUND, BENCH_ID } from './bench-marker';
${IMPORT_LIST}
import './styles/App.css';

export const App: React.FC = () => {
  return (
    <div className="app-container" data-testid="app">
      <header className="app-header">
        <h1>Coding Bench — Round {BENCH_ROUND} ({BENCH_ID})</h1>
      </header>
      <div className="app-grid">
${COMPONENT_RENDER_LIST}
      </div>
    </div>
  );
};
APPEOF

cat > src/index.tsx << ENTRYEOF
import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';

const root = createRoot(document.getElementById('root')!);
root.render(<App />);
ENTRYEOF

echo "[7/8] App.tsx and index.tsx created"

# -------------------------------------------------------
# 8. Install dependencies, run initial build, init git
# -------------------------------------------------------
echo "[8/8] Installing npm dependencies..."
npm install --registry=https://registry.npmmirror.com --strict-ssl=false 2>&1 | tail -5

echo "Running initial production build..."
npm run build 2>&1 | tail -10

echo "Initializing git repository..."
git init
git add -A
git commit -m "Initial coding-bench project" --no-gpg-sign

echo ""
echo "=== Coding benchmark project setup complete ==="
echo "  Project:     ${PROJECT_DIR}"
echo "  Components:  ${COMPONENT_COUNT}"
echo "  Build cmd:   cd ${PROJECT_DIR} && npm run build"
echo "  Test cmd:    cd ${PROJECT_DIR} && npm test"
echo "  Modify cmd:  sed -i \"s/export const BENCH_ROUND = .*/export const BENCH_ROUND = {N};/\" ${PROJECT_DIR}/src/bench-marker.ts"
echo ""
