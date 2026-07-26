#!/bin/bash
# Setup React + webpack5 + MUI coding benchmark project inside Docker image
# Generates a project with 80 React components using @mui/material for
# memory-intensive webpack builds (~3GB peak memory per build)
#
# Memory pressure: each component imports and renders 20+ MUI widgets,
# forcing webpack to process hundreds of internal modules.
# @mui/icons-material adds ~2000+ icon modules to the barrel analysis.
#
# Usage: Called from Dockerfile.coding during docker build
# Result: /opt/coding-bench/ with pre-installed node_modules and initial build

set -e

PROJECT_DIR="/opt/coding-bench"
COMPONENT_COUNT=80

echo "=== Setting up coding benchmark project ==="
echo "  Project dir: ${PROJECT_DIR}"
echo "  Components:  ${COMPONENT_COUNT}"

mkdir -p "${PROJECT_DIR}"
cd "${PROJECT_DIR}"

# -------------------------------------------------------
# 1. Create package.json (with MUI + emotion dependencies)
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
    "react-dom": "^18.3.1",
    "@mui/material": "^6.0.0",
    "@mui/icons-material": "^6.0.0",
    "@emotion/react": "^11.13.0",
    "@emotion/styled": "^11.13.0"
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

echo "[1/8] package.json created (with MUI + icons + emotion)"

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
        mui: {
          test: /[\\/]@mui[\\/]/,
          name: 'mui-vendors',
          chunks: 'all',
        },
      },
    },
    runtimeChunk: 'single',
  },
  performance: {
    hints: false,
  },
};
WEBPACK

echo "[2/8] webpack.config.js created (with MUI cache group)"

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
};
JEST

echo "[4/8] jest.config.js created"

# -------------------------------------------------------
# 5. Create source directory structure and core files
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

# App.css — minimal (MUI handles most styling)
cat > src/styles/App.css << 'CSS'
.app-container { padding: 16px; }
.app-header { margin-bottom: 16px; }
.app-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
CSS

echo "[5b/8] App.css created"

# Utils (same as before)
cat > src/utils/helpers.ts << 'HELPERS'
export function formatNumber(n: number): string { return n.toLocaleString(); }
export function generateId(): string { return Math.random().toString(36).substring(2, 9); }
export function truncate(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str; return str.substring(0, maxLen) + '...';
}
export const ITEMS = ['Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta', 'Eta', 'Theta', 'Iota', 'Kappa'];
HELPERS

cat > src/utils/api.ts << 'API'
export interface ApiResponse<T> { data: T; status: number; message: string; }
export async function fetchData<T>(url: string): Promise<ApiResponse<T>> {
  return { data: {} as T, status: 200, message: 'OK' };
}
export const API_ENDPOINTS = { users: '/api/users', posts: '/api/posts', comments: '/api/comments', settings: '/api/settings' };
API

cat > src/utils/format.ts << 'FORMAT'
export function formatDate(date: Date): string { return date.toISOString().split('T')[0]; }
export function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(amount);
}
export function formatPercent(value: number): string { return `${(value * 100).toFixed(1)}%`; }
FORMAT

echo "[5c/8] utility modules created"

# -------------------------------------------------------
# 6. Generate 80 React components with MUI imports
# -------------------------------------------------------
# 5 import groups cycling across 80 components.
# Each group imports a different MUI subset to maximize module coverage.
# All imports are USED in JSX (no tree shaking).

# Group 0: Card/Button/Typography + common widgets
MUI_IMPORTS_0='{ Box, Card, CardContent, CardActions, Button, Typography, Chip, IconButton, TextField, Switch, Slider, Badge, Alert, Tooltip, Divider, Grid, Stack, Paper }'
ICON_IMPORTS_0='{ Favorite, Share, Delete, Settings, Notifications }'

# Group 1: Navigation/Tabs/Stepper
MUI_IMPORTS_1='{ Box, Tabs, Tab, Stepper, Step, StepLabel, StepContent, AppBar, Toolbar, Drawer, Breadcrumbs, Link, LinearProgress, CircularProgress, Snackbar, Paper, Typography, Button, IconButton }'
ICON_IMPORTS_1='{ NavigateNext, NavigateBefore, Home, Menu, ArrowBack, Close }'

# Group 2: Table/Form/Select
MUI_IMPORTS_2='{ Box, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TablePagination, Select, FormControl, InputLabel, MenuItem, FormHelperText, Checkbox, FormControlLabel, Radio, RadioGroup, Rating, Paper, Typography }'
ICON_IMPORTS_2='{ Star, StarBorder, FilterList, Sort, Edit, Save, Check }'

# Group 3: Dialog/Accordion/List
MUI_IMPORTS_3='{ Box, Dialog, DialogTitle, DialogContent, DialogContentText, DialogActions, Accordion, AccordionSummary, AccordionDetails, List, ListItem, ListItemText, ListItemIcon, ListItemButton, Collapse, Fade, Paper, Typography, Button, IconButton }'
ICON_IMPORTS_3='{ ExpandMore, Info, Warning, Error, CheckCircle, Cancel }'

# Group 4: Avatar/Badge/Fab/Progress
MUI_IMPORTS_4='{ Box, Avatar, AvatarGroup, Badge, Fab, SpeedDial, SpeedDialAction, SpeedDialIcon, BottomNavigation, BottomNavigationAction, ToggleButton, ToggleButtonGroup, Progress, Paper, Typography, Button, IconButton, Chip }'
ICON_IMPORTS_4='{ Add, Remove, Search, Print, Refresh, MoreVert }'

COMPONENT_NAMES=(
  "Dashboard" "Header" "Footer" "Sidebar" "Navigation"
  "LoginForm" "RegisterForm" "UserProfile" "SettingsPanel" "NotificationBar"
  "DataTable" "ChartView" "PieChart" "LineGraph" "BarChart"
  "SearchBar" "FilterPanel" "SortControl" "Pagination" "InfiniteList"
  "ModalDialog" "ConfirmDialog" "AlertDialog" "TooltipView" "PopoverMenu"
  "Accordion" "TabView" "Breadcrumb" "StepperControl" "ProgressBar"
  "FileUploader" "ImageGallery" "VideoPlayer" "AudioPlayer" "CarouselView"
  "CodeEditor" "MarkdownView" "DiffViewer" "TerminalView" "TaskBoard"
  "FormPanel" "InputField" "DatePicker" "TimePicker" "ColorPicker"
  "TagList" "CategoryNav" "RatingView" "VotePanel" "CommentSection"
  "ChatWindow" "MessageList" "ContactCard" "ProfileEditor" "AvatarPicker"
  "BadgeDisplay" "SpeedDialMenu" "FabActions" "BottomNav" "ToggleGroup"
  "CollapsePanel" "FadeTransition" "GrowAnimation" "SlidePanel" "ZoomView"
  "TimelineView" "TreeView" "OrgChart" "KanbanBoard" "CalendarView"
  "MapViewer" "WeatherWidget" "StockChart" "NewsFeed" "SocialShare"
  "BookmarkList" "HistoryLog" "ActivityFeed" "StatusMonitor" "HealthCheck"
)

IMPORT_LIST=""
COMPONENT_RENDER_LIST=""

for i in $(seq 0 $((${COMPONENT_COUNT} - 1))); do
    NAME="${COMPONENT_NAMES[$i]}"
    GROUP=$((i % 5))

    # Pick MUI imports for this group
    case $GROUP in
      0) MUI_IMPORTS="$MUI_IMPORTS_0"; ICON_IMPORTS="$ICON_IMPORTS_0" ;;
      1) MUI_IMPORTS="$MUI_IMPORTS_1"; ICON_IMPORTS="$ICON_IMPORTS_1" ;;
      2) MUI_IMPORTS="$MUI_IMPORTS_2"; ICON_IMPORTS="$ICON_IMPORTS_2" ;;
      3) MUI_IMPORTS="$MUI_IMPORTS_3"; ICON_IMPORTS="$ICON_IMPORTS_3" ;;
      4) MUI_IMPORTS="$MUI_IMPORTS_4"; ICON_IMPORTS="$ICON_IMPORTS_4" ;;
    esac

    # Generate component file with MUI widgets actually rendered
    cat > "src/components/${NAME}.tsx" << COMPFILE
import React, { useState } from 'react';
import { BENCH_ROUND, BENCH_ID } from '../bench-marker';
import { formatNumber, ITEMS } from '../utils/helpers';
import ${MUI_IMPORTS} from '@mui/material';
import ${ICON_IMPORTS} from '@mui/icons-material';

export const ${NAME}: React.FC = () => {
  const [count, setCount] = useState(0);
  const [checked, setChecked] = useState(false);
  const [sliderVal, setSliderVal] = useState(${i} * 10 % 100);
  const [inputVal, setInputVal] = useState('');

  return (
    <Card data-testid="${NAME}" sx={{ maxWidth: 380 }}>
      <CardContent>
        <Typography variant="h6">${NAME} (R{BENCH_ROUND})</Typography>
        <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
          <Chip label="Active" color="primary" size="small" />
          <Chip label="Idle" color="default" size="small" />
          <Badge badgeContent={count} color="secondary">
            <IconButton size="small"><Settings /></IconButton>
          </Badge>
        </Stack>
        <Divider />
        <Box sx={{ mt: 1 }}>
          <TextField
            fullWidth label="Search" variant="outlined" size="small"
            value={inputVal} onChange={e => setInputVal(e.target.value)}
          />
          <Slider value={sliderVal} onChange={(_, v) => setSliderVal(v as number)} sx={{ mt: 1 }} />
          <Switch checked={checked} onChange={e => setChecked(e.target.checked)} />
        </Box>
        {checked && <Alert severity="info" sx={{ mt: 1 }}>Status: active, count={count}</Alert>}
        <Tooltip title="Detail info">
          <Paper sx={{ p: 1, mt: 1 }}>Score: {formatNumber(sliderVal)}</Paper>
        </Tooltip>
      </CardContent>
      <CardActions>
        <Button size="small" variant="contained" onClick={() => setCount(count + 1)}>Like</Button>
        <IconButton size="small"><Favorite /></IconButton>
        <IconButton size="small"><Share /></IconButton>
        <IconButton size="small"><Delete /></IconButton>
        <IconButton size="small"><Notifications /></IconButton>
      </CardActions>
    </Card>
  );
};
COMPFILE

    IMPORT_LIST="${IMPORT_LIST}import { ${NAME} } from './components/${NAME}';
"
    COMPONENT_RENDER_LIST="${COMPONENT_RENDER_LIST}          <${NAME} />
"

    # Generate test file for every 8th component (10 test files)
    if [ $((i % 8)) -eq 0 ]; then
        cat > "src/__tests__/${NAME}.test.tsx" << TESTFILE
import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { ${NAME} } from '../components/${NAME}';

describe('${NAME}', () => {
  test('renders component card', () => {
    render(<${NAME} />);
    const card = screen.getByTestId('${NAME}');
    expect(card).toBeInTheDocument();
  });

  test('displays component name', () => {
    render(<${NAME} />);
    expect(screen.getByText(/${NAME}/)).toBeInTheDocument();
  });

  test('has like button', () => {
    render(<${NAME} />);
    expect(screen.getByRole('button', { name: /like/i })).toBeInTheDocument();
  });
});
TESTFILE
    fi
done

echo "[6/8] ${COMPONENT_COUNT} component files generated (with MUI imports)"

# -------------------------------------------------------
# 7. Create App.tsx and index.tsx
# -------------------------------------------------------
cat > src/App.tsx << APPEOF
import React from 'react';
import { BENCH_ROUND, BENCH_ID } from './bench-marker';
import { CssBaseline, ThemeProvider, createTheme, Container, AppBar, Toolbar, Typography, Box } from '@mui/material';
${IMPORT_LIST}
import './styles/App.css';

const theme = createTheme({
  palette: { primary: { main: '#667eea' }, secondary: { main: '#764ba2' } },
});

export const App: React.FC = () => {
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Container maxWidth="xl" data-testid="app">
        <AppBar position="static" sx={{ mb: 2, borderRadius: 1 }}>
          <Toolbar>
            <Typography variant="h6">Coding Bench — Round {BENCH_ROUND} ({BENCH_ID})</Typography>
          </Toolbar>
        </AppBar>
        <Box className="app-grid">
${COMPONENT_RENDER_LIST}
        </Box>
      </Container>
    </ThemeProvider>
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
echo "[8/8] Installing npm dependencies (includes @mui/material + icons + emotion)..."
npm install --registry=https://registry.npmmirror.com --strict-ssl=false 2>&1 | tail -5

echo "Running initial production build (expect ~3GB peak memory)..."
npm run build 2>&1 | tail -10

echo "Initializing git repository..."
git init
git config user.email "bench@coding-bench.local"
git config user.name "Coding Bench"
git add -A
git commit -m "Initial coding-bench project with MUI (80 components)" --no-gpg-sign

echo ""
echo "=== Coding benchmark project setup complete ==="
echo "  Project:     ${PROJECT_DIR}"
echo "  Components:  ${COMPONENT_COUNT}"
echo "  MUI imports: 5 groups cycling across components"
echo "  Build cmd:   cd ${PROJECT_DIR} && npm run build"
echo "  Test cmd:    cd ${PROJECT_DIR} && npm test"
echo "  Modify cmd:  sed -i \"s/export const BENCH_ROUND = .*/export const BENCH_ROUND = {N};/\" ${PROJECT_DIR}/src/bench-marker.ts"
echo ""
