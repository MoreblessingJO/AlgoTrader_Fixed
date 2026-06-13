# Mobile App Setup

## Prerequisites
- Node.js 18+
- Expo CLI: `npm install -g expo-cli`
- iOS: Xcode + Apple Developer account
- Android: Android Studio + Google Play account

## Quick start

```bash
# Create Expo project
npx create-expo-app TradingApp
cd TradingApp

# Install dependencies
npm install @react-navigation/native @react-navigation/bottom-tabs \
            @react-navigation/stack react-native-chart-kit \
            react-native-svg expo-secure-store expo-notifications \
            react-native-screens react-native-safe-area-context

# Copy app file
cp ../trading_system/mobile/App.js ./App.js

# Set your server URL in App.js
# Line 16: const API_BASE = 'https://your-domain.com';

# Run on device/simulator
npx expo start
```

## Build for stores

```bash
# iOS
npx expo build:ios

# Android
npx expo build:android
```

## Push notifications setup

1. Firebase (Android): create project at console.firebase.google.com
   - Add Android app → download google-services.json → place in project root

2. APNs (iOS): configure in Apple Developer portal
   - Certificates → push notification certificate

3. In bot.py, add FCM/APNs push on trade events:
   ```python
   from monitor.push import send_push
   await send_push(title="Trade opened", body=f"{order.strategy} {order.side} {order.symbol}")
   ```

## Feature list

| Screen        | Features                                           |
|---------------|----------------------------------------------------|
| Dashboard     | Live balance, P&L, open positions, market stats    |
| Trades        | Full history, filters by market/strategy           |
| Performance   | Strategy breakdown, WR vs backtest, divergences    |
| Controls      | Halt/resume bot, emergency close all               |
| Notifications | Push on every trade open/close, circuit breaker    |
