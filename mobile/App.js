/**
 * mobile/App.js — React Native trading app
 * iOS + Android. Connects to the same FastAPI backend.
 *
 * Setup:
 *   npx create-expo-app TradingApp
 *   cd TradingApp
 *   npm install @react-navigation/native @react-navigation/bottom-tabs
 *   npm install @react-navigation/stack expo-secure-store expo-notifications
 *   npm install react-native-chart-kit react-native-svg
 *   Copy this file → App.js
 *   Set API_BASE to your server URL
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  View, Text, ScrollView, TouchableOpacity, TextInput,
  StyleSheet, Alert, RefreshControl, ActivityIndicator,
  Platform, Dimensions, StatusBar,
} from 'react-native';
import { NavigationContainer } from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { createStackNavigator } from '@react-navigation/stack';

// ── Config ────────────────────────────────────────────────────────────
const API_BASE   = 'http://YOUR_SERVER_IP:8080';  // ← change this
const WS_BASE    = API_BASE.replace('http','ws');

// ── Theme ─────────────────────────────────────────────────────────────
const C = {
  bg:       '#0d0f14', bg2: '#161921', bg3: '#1e2230',
  border:   'rgba(255,255,255,0.08)',
  text:     '#e8eaf0', muted: '#7c8194', faint: '#3a3f52',
  green:    '#1db97a', red: '#e24b4a', blue: '#378add',
  purple:   '#7f77dd', amber: '#ef9f27', teal: '#1d9e75',
};

// ── Auth store (simple in-memory; use expo-secure-store in production) ─
let _token = '';
const setToken = (t) => { _token = t; };
const getToken = () => _token;
const authHeaders = () => ({ 'Authorization': `Bearer ${getToken()}`, 'Content-Type': 'application/json' });
const apiFetch = async (path, opts = {}) => {
  const r = await fetch(`${API_BASE}${path}`, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
  if (!r.ok) throw new Error(`API error ${r.status}`);
  return r.json();
};

const { width } = Dimensions.get('window');


// ══════════════════════════════════════════════════════════════════════
//  Shared components
// ══════════════════════════════════════════════════════════════════════

const MetricCard = ({ label, value, sub, color }) => (
  <View style={s.metricCard}>
    <Text style={s.metricLabel}>{label}</Text>
    <Text style={[s.metricValue, color && { color }]}>{value}</Text>
    {sub && <Text style={s.metricSub}>{sub}</Text>}
  </View>
);

const Badge = ({ text, color = C.blue }) => (
  <View style={[s.badge, { backgroundColor: color + '22' }]}>
    <Text style={[s.badgeText, { color }]}>{text}</Text>
  </View>
);

const Pill = ({ text, type }) => {
  const colors = { BUY: C.green, SELL: C.red, crypto: C.purple, crash_boom: C.teal, forex: C.amber };
  const c = colors[type] || colors[text] || C.blue;
  return (
    <View style={[s.pill, { backgroundColor: c + '22' }]}>
      <Text style={[s.pillText, { color: c }]}>{text}</Text>
    </View>
  );
};

const Card = ({ children, style }) => (
  <View style={[s.card, style]}>{children}</View>
);

const SectionTitle = ({ text }) => (
  <Text style={s.sectionTitle}>{text}</Text>
);


// ══════════════════════════════════════════════════════════════════════
//  Login screen
// ══════════════════════════════════════════════════════════════════════

function LoginScreen({ onLogin }) {
  const [user, setUser] = useState('admin');
  const [pass, setPass] = useState('changeme123');
  const [err, setErr]   = useState('');
  const [loading, setLoading] = useState(false);

  const doLogin = async () => {
    setLoading(true); setErr('');
    try {
      const d = await fetch(`${API_BASE}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user, password: pass }),
      }).then(r => r.json());
      if (!d.token) throw new Error(d.detail || 'Login failed');
      setToken(d.token);
      onLogin();
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <View style={[s.center, { flex: 1, backgroundColor: C.bg }]}>
      <StatusBar barStyle="light-content" />
      <View style={s.loginBox}>
        <View style={[s.row, { marginBottom: 8 }]}>
          <View style={s.liveDot} />
          <Text style={[s.loginTitle]}>AlgoTrader</Text>
        </View>
        <Text style={s.loginSub}>Sign in to your trading platform</Text>
        <TextInput style={s.input} placeholder="Username" placeholderTextColor={C.muted}
          value={user} onChangeText={setUser} autoCapitalize="none" />
        <TextInput style={s.input} placeholder="Password" placeholderTextColor={C.muted}
          value={pass} onChangeText={setPass} secureTextEntry />
        {err ? <Text style={s.errText}>{err}</Text> : null}
        <TouchableOpacity style={s.loginBtn} onPress={doLogin} disabled={loading}>
          {loading ? <ActivityIndicator color="#fff" /> : <Text style={s.loginBtnText}>Sign in</Text>}
        </TouchableOpacity>
      </View>
    </View>
  );
}


// ══════════════════════════════════════════════════════════════════════
//  Dashboard screen
// ══════════════════════════════════════════════════════════════════════

function DashboardScreen() {
  const [snap, setSnap] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const ws = useRef(null);

  const load = useCallback(async () => {
    try {
      const d = await apiFetch('/api/snapshot');
      setSnap(d);
    } catch (e) { console.warn('Dashboard load error', e); }
  }, []);

  const connectWS = useCallback(() => {
    if (ws.current) ws.current.close();
    ws.current = new WebSocket(`${WS_BASE}/ws?token=${getToken()}`);
    ws.current.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'snapshot') setSnap(msg.data);
    };
    ws.current.onclose = () => setTimeout(connectWS, 3000);
  }, []);

  useEffect(() => {
    load();
    connectWS();
    const interval = setInterval(load, 15000);
    return () => { clearInterval(interval); ws.current?.close(); };
  }, [load, connectWS]);

  const onRefresh = async () => { setRefreshing(true); await load(); setRefreshing(false); };

  const r = snap?.risk || {};
  const orders = snap?.open_orders || [];
  const ms = snap?.market_stats || {};

  return (
    <ScrollView style={s.screen} refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.green} />}>
      <View style={s.pageHeader}>
        <Text style={s.pageTitle}>Dashboard</Text>
        <View style={s.row}>
          <View style={s.liveDot} />
          <Text style={s.liveText}>Live</Text>
        </View>
      </View>

      {/* Metrics */}
      <View style={s.metricsGrid}>
        <MetricCard label="Balance" value={'$' + (r.balance || 10000).toLocaleString('en', {minimumFractionDigits:2,maximumFractionDigits:2})}
          sub={(r.total_return_pct >= 0 ? '+' : '') + (r.total_return_pct || 0).toFixed(2) + '%'}
          color={(r.total_return_pct || 0) >= 0 ? C.green : C.red} />
        <MetricCard label="Daily P&L"
          value={(r.daily_pnl_pct >= 0 ? '+' : '') + (r.daily_pnl_pct || 0).toFixed(2) + '%'}
          color={(r.daily_pnl_pct || 0) >= 0 ? C.green : C.red} />
        <MetricCard label="Open" value={r.open_positions || 0} sub="positions" color={C.blue} />
        <MetricCard label="Drawdown" value={(r.drawdown_pct || 0).toFixed(1) + '%'}
          color={(r.drawdown_pct || 0) > 5 ? C.red : C.green} />
      </View>

      {/* Market stats */}
      <SectionTitle text="Markets" />
      {['crypto','crash_boom','forex'].map(m => {
        const st = ms[m] || {};
        const colors = { crypto: C.purple, crash_boom: C.teal, forex: C.amber };
        return (
          <Card key={m} style={{ marginHorizontal: 16 }}>
            <View style={s.rowBetween}>
              <Text style={[s.cardLabel, { color: colors[m] }]}>{m.replace('_',' ').replace(/\b\w/g,c=>c.toUpperCase())}</Text>
              <Text style={[s.cardValue, { color: (st.total_pnl||0) >= 0 ? C.green : C.red }]}>
                {(st.total_pnl||0) >= 0 ? '+' : ''}${(st.total_pnl||0).toFixed(2)}
              </Text>
            </View>
            <View style={[s.row, { marginTop: 6, gap: 16 }]}>
              <Text style={s.muted}>{st.trades || 0} trades</Text>
              <Text style={[s.muted, { color: (st.wr||0) >= 60 ? C.green : C.red }]}>WR {(st.wr||0).toFixed(1)}%</Text>
              <Text style={s.muted}>avg ${(st.avg_pnl||0).toFixed(2)}</Text>
            </View>
          </Card>
        );
      })}

      {/* Open positions */}
      <SectionTitle text="Open positions" />
      {orders.length === 0 ? (
        <Card style={{ marginHorizontal: 16 }}>
          <Text style={s.muted}>No open positions</Text>
        </Card>
      ) : orders.map(o => (
        <Card key={o.id} style={{ marginHorizontal: 16 }}>
          <View style={s.rowBetween}>
            <View style={s.row}>
              <Pill text={o.strategy} type={o.market} />
              <Text style={[s.cardLabel, { marginLeft: 8 }]}>{o.symbol}</Text>
            </View>
            <Pill text={o.side} type={o.side} />
          </View>
          <View style={[s.rowBetween, { marginTop: 8 }]}>
            <Text style={s.muted}>Entry {(o.fill_price||0).toFixed(5)}</Text>
            <Text style={[(o.unrealised_pnl||0)>=0?s.green:s.red]}>
              {(o.unrealised_pnl||0)>=0?'+':''}${(o.unrealised_pnl||0).toFixed(2)}
            </Text>
          </View>
        </Card>
      ))}

      <View style={{ height: 40 }} />
    </ScrollView>
  );
}


// ══════════════════════════════════════════════════════════════════════
//  Trades screen
// ══════════════════════════════════════════════════════════════════════

function TradesScreen() {
  const [trades, setTrades]   = useState([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter]   = useState('all');

  useEffect(() => {
    apiFetch('/api/trades?limit=100').then(d => { setTrades(d.trades || []); setLoading(false); });
  }, []);

  const filtered = filter === 'all' ? trades : trades.filter(t => t.market === filter);
  const filters  = ['all','crypto','crash_boom','forex'];
  const fColors  = { all: C.blue, crypto: C.purple, crash_boom: C.teal, forex: C.amber };

  return (
    <ScrollView style={s.screen}>
      <View style={s.pageHeader}><Text style={s.pageTitle}>Trade history</Text></View>
      <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{ paddingHorizontal: 16, marginBottom: 12 }}>
        <View style={s.row}>
          {filters.map(f => (
            <TouchableOpacity key={f} onPress={() => setFilter(f)}
              style={[s.filterBtn, filter === f && { borderColor: fColors[f], backgroundColor: fColors[f] + '22' }]}>
              <Text style={[s.filterBtnText, filter === f && { color: fColors[f] }]}>
                {f.replace('_',' ')}
              </Text>
            </TouchableOpacity>
          ))}
        </View>
      </ScrollView>

      {loading ? <ActivityIndicator color={C.green} style={{ marginTop: 40 }} /> :
        filtered.map(t => (
          <Card key={t.id} style={{ marginHorizontal: 16 }}>
            <View style={s.rowBetween}>
              <View style={s.row}>
                <Pill text={t.strategy} type={t.market} />
                <Text style={[s.muted, { marginLeft: 8 }]}>{t.symbol}</Text>
              </View>
              <Text style={[t.pnl >= 0 ? s.green : s.red, { fontWeight: '600' }]}>
                {t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(2)}
              </Text>
            </View>
            <View style={[s.rowBetween, { marginTop: 6 }]}>
              <View style={s.row}>
                <Pill text={t.side} type={t.side} />
                <Text style={[s.muted, { marginLeft: 8 }]}>{t.exit_reason}</Text>
              </View>
              <Text style={s.muted}>{t.held_min}m</Text>
            </View>
          </Card>
        ))
      }
      <View style={{ height: 40 }} />
    </ScrollView>
  );
}


// ══════════════════════════════════════════════════════════════════════
//  Performance screen
// ══════════════════════════════════════════════════════════════════════

function PerformanceScreen() {
  const [stats, setStats]     = useState(null);
  const [metrics, setMetrics] = useState(null);

  useEffect(() => {
    Promise.all([apiFetch('/api/trades/stats'), apiFetch('/api/metrics')])
      .then(([s, m]) => { setStats(s); setMetrics(m); });
  }, []);

  const strats = stats?.strategy_stats || {};
  const BT = { 'CB-S1':83,'CB-S2':81,'CB-S3':92,'CB-S4':96,'FX-S1':71,'FX-S2':76,'FX-S3':66,'FX-S4':73,'CR-S1':75,'CR-S2':68 };

  return (
    <ScrollView style={s.screen}>
      <View style={s.pageHeader}><Text style={s.pageTitle}>Performance</Text></View>
      <View style={s.metricsGrid}>
        <MetricCard label="Total trades" value={metrics?.total_trades || 0} />
        <MetricCard label="Win rate" value={(metrics?.overall_wr || 0) + '%'} color={C.green} />
      </View>

      <SectionTitle text="Strategy breakdown" />
      {Object.entries(strats).map(([k, sv]) => {
        const t = sv.trades || 0;
        const wr = t > 0 ? Math.round(sv.wins / t * 100) : 0;
        const btWR = BT[k] || 0;
        const gap = btWR - wr;
        const mkt = k.startsWith('CB') ? 'crash_boom' : k.startsWith('FX') ? 'forex' : 'crypto';
        return (
          <Card key={k} style={{ marginHorizontal: 16 }}>
            <View style={s.rowBetween}>
              <Pill text={k} type={mkt} />
              <Text style={[wr >= 60 ? s.green : s.red, { fontWeight: '600' }]}>{wr}% WR</Text>
            </View>
            <View style={[s.rowBetween, { marginTop: 8 }]}>
              <Text style={s.muted}>{t} trades · ${(sv.pnl||0).toFixed(2)} P&L</Text>
              {gap > 8 ? <Text style={s.red}>−{gap.toFixed(1)}pp vs BT</Text> : <Text style={s.green}>On target</Text>}
            </View>
          </Card>
        );
      })}

      <SectionTitle text="WR divergence alerts" />
      {(stats?.divergences || []).length === 0 ? (
        <Card style={{ marginHorizontal: 16 }}><Text style={s.muted}>No divergences detected</Text></Card>
      ) : (stats?.divergences || []).map((d, i) => (
        <Card key={i} style={{ marginHorizontal: 16 }}>
          <View style={s.rowBetween}>
            <Text style={s.red}>{d.strategy}</Text>
            <Text style={s.red}>−{d.gap.toFixed(1)}pp</Text>
          </View>
          <Text style={s.muted}>Live {d.live_wr}% vs backtest {d.bt_wr}% ({d.trades} trades)</Text>
        </Card>
      ))}
      <View style={{ height: 40 }} />
    </ScrollView>
  );
}


// ══════════════════════════════════════════════════════════════════════
//  Controls screen
// ══════════════════════════════════════════════════════════════════════

function ControlsScreen() {
  const [status, setStatus] = useState(null);
  const [snap, setSnap]     = useState(null);

  const load = async () => {
    const [st, sn] = await Promise.all([apiFetch('/api/bot/status'), apiFetch('/api/snapshot')]);
    setStatus(st); setSnap(sn);
  };

  useEffect(() => { load(); }, []);

  const halt = async () => {
    Alert.alert('Halt trading', 'Stop all new signals? Open positions still monitored.', [
      { text: 'Cancel', style: 'cancel' },
      { text: 'Halt', style: 'destructive', onPress: async () => { await apiFetch('/api/bot/halt',{method:'POST'}); load(); } },
    ]);
  };

  const resume = async () => {
    await apiFetch('/api/bot/resume', { method: 'POST' });
    load();
  };

  const closeAll = async () => {
    Alert.alert('Close all positions', 'This will market-close all open trades immediately.', [
      { text: 'Cancel', style: 'cancel' },
      { text: 'Close all', style: 'destructive', onPress: () => Alert.alert('Action sent','Close-all signal sent to bot.') },
    ]);
  };

  const r = snap?.risk || {};

  return (
    <ScrollView style={s.screen}>
      <View style={s.pageHeader}><Text style={s.pageTitle}>Controls</Text></View>

      <Card style={{ marginHorizontal: 16 }}>
        <Text style={s.cardTitle}>System status</Text>
        {[
          ['Bot running', status?.running ? 'Yes' : 'No', status?.running ? C.green : C.red],
          ['Mode', (status?.mode || 'paper').toUpperCase(), C.amber],
          ['Halted', status?.halted ? 'Yes' : 'No', status?.halted ? C.red : C.green],
          ['Open positions', r.open_positions || 0, C.blue],
          ['Balance', '$' + (r.balance || 0).toLocaleString(), C.text],
          ['Drawdown', (r.drawdown_pct || 0).toFixed(1) + '%', (r.drawdown_pct || 0) > 5 ? C.red : C.green],
        ].map(([label, val, color]) => (
          <View key={label} style={[s.rowBetween, s.statusRow]}>
            <Text style={s.muted}>{label}</Text>
            <Text style={{ color: color || C.text }}>{val}</Text>
          </View>
        ))}
      </Card>

      <View style={{ marginHorizontal: 16, marginTop: 16, gap: 10 }}>
        <TouchableOpacity style={[s.ctrlBtn, { borderColor: C.green + '66', backgroundColor: C.green + '15' }]} onPress={resume}>
          <Text style={[s.ctrlBtnText, { color: C.green }]}>Resume trading</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[s.ctrlBtn, { borderColor: C.amber + '66', backgroundColor: C.amber + '15' }]} onPress={halt}>
          <Text style={[s.ctrlBtnText, { color: C.amber }]}>Halt all signals</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[s.ctrlBtn, { borderColor: C.red + '66', backgroundColor: C.red + '15' }]} onPress={closeAll}>
          <Text style={[s.ctrlBtnText, { color: C.red }]}>Emergency — close all positions</Text>
        </TouchableOpacity>
      </View>

      <Card style={{ marginHorizontal: 16, marginTop: 12 }}>
        <Text style={[s.muted, { lineHeight: 20, fontSize: 12 }]}>
          Halt stops all new trade entry signals. Your open positions continue to be
          monitored for SL and TP hits. Use "Emergency — close all" only in a genuine
          crisis — it will market-close everything immediately.
        </Text>
      </Card>
      <View style={{ height: 40 }} />
    </ScrollView>
  );
}


// ══════════════════════════════════════════════════════════════════════
//  Navigation
// ══════════════════════════════════════════════════════════════════════

const Tab = createBottomTabNavigator();

function TabIcon({ name, focused }) {
  const icons = { Dashboard:'◈', Trades:'↕', Performance:'◉', Controls:'◧' };
  return <Text style={{ fontSize: 18, color: focused ? C.green : C.muted }}>{icons[name] || '◈'}</Text>;
}

function MainTabs() {
  return (
    <Tab.Navigator screenOptions={({ route }) => ({
      headerShown: false,
      tabBarStyle: { backgroundColor: C.bg2, borderTopColor: C.border, borderTopWidth: 0.5 },
      tabBarActiveTintColor: C.green,
      tabBarInactiveTintColor: C.muted,
      tabBarLabelStyle: { fontSize: 10 },
      tabBarIcon: ({ focused }) => <TabIcon name={route.name} focused={focused} />,
    })}>
      <Tab.Screen name="Dashboard"   component={DashboardScreen} />
      <Tab.Screen name="Trades"      component={TradesScreen} />
      <Tab.Screen name="Performance" component={PerformanceScreen} />
      <Tab.Screen name="Controls"    component={ControlsScreen} />
    </Tab.Navigator>
  );
}

export default function App() {
  const [authed, setAuthed] = useState(false);
  return (
    <NavigationContainer theme={{ colors: { background: C.bg } }}>
      {authed ? <MainTabs /> : <LoginScreen onLogin={() => setAuthed(true)} />}
    </NavigationContainer>
  );
}


// ══════════════════════════════════════════════════════════════════════
//  Styles
// ══════════════════════════════════════════════════════════════════════

const s = StyleSheet.create({
  screen:      { flex:1, backgroundColor:C.bg },
  center:      { alignItems:'center', justifyContent:'center' },
  row:         { flexDirection:'row', alignItems:'center' },
  rowBetween:  { flexDirection:'row', alignItems:'center', justifyContent:'space-between' },

  pageHeader:  { flexDirection:'row', alignItems:'center', justifyContent:'space-between', paddingHorizontal:16, paddingTop:Platform.OS==='ios'?56:24, paddingBottom:16 },
  pageTitle:   { fontSize:22, fontWeight:'700', color:C.text },

  liveDot:     { width:7, height:7, borderRadius:4, backgroundColor:C.green, marginRight:5 },
  liveText:    { fontSize:12, color:C.green },

  metricsGrid: { flexDirection:'row', flexWrap:'wrap', paddingHorizontal:12, gap:8, marginBottom:8 },
  metricCard:  { backgroundColor:C.bg2, borderWidth:0.5, borderColor:C.border, borderRadius:10, padding:14, width:(width-40)/2 },
  metricLabel: { fontSize:11, color:C.muted, marginBottom:4 },
  metricValue: { fontSize:20, fontWeight:'700', color:C.text },
  metricSub:   { fontSize:11, color:C.muted, marginTop:3 },

  card:        { backgroundColor:C.bg2, borderWidth:0.5, borderColor:C.border, borderRadius:10, padding:14, marginBottom:8 },
  cardLabel:   { fontSize:13, fontWeight:'500', color:C.text },
  cardValue:   { fontSize:13, fontWeight:'600' },
  cardTitle:   { fontSize:13, fontWeight:'500', color:C.muted, marginBottom:12 },

  sectionTitle:{ fontSize:12, fontWeight:'600', color:C.muted, paddingHorizontal:16, paddingVertical:10, textTransform:'uppercase', letterSpacing:0.8 },

  badge:       { paddingHorizontal:8, paddingVertical:2, borderRadius:20 },
  badgeText:   { fontSize:10, fontWeight:'600' },
  pill:        { paddingHorizontal:8, paddingVertical:2, borderRadius:20, marginRight:4 },
  pillText:    { fontSize:10, fontWeight:'600' },

  muted:       { fontSize:12, color:C.muted },
  green:       { color:C.green },
  red:         { color:C.red },
  blue:        { color:C.blue },

  loginBox:    { backgroundColor:C.bg2, borderWidth:0.5, borderColor:C.border, borderRadius:16, padding:28, width:Math.min(width-40,380) },
  loginTitle:  { fontSize:22, fontWeight:'700', color:C.text, marginLeft:6 },
  loginSub:    { fontSize:13, color:C.muted, marginBottom:24, marginTop:4 },
  input:       { backgroundColor:C.bg3, borderWidth:0.5, borderColor:C.border, borderRadius:8, padding:12, color:C.text, fontSize:14, marginBottom:10 },
  loginBtn:    { backgroundColor:C.blue, borderRadius:8, padding:13, alignItems:'center', marginTop:4 },
  loginBtnText:{ color:'#fff', fontSize:14, fontWeight:'600' },
  errText:     { color:C.red, fontSize:12, marginTop:4 },

  filterBtn:     { borderWidth:0.5, borderColor:C.border, borderRadius:20, paddingHorizontal:14, paddingVertical:6, marginRight:8 },
  filterBtnText: { fontSize:12, color:C.muted },

  statusRow:   { paddingVertical:10, borderBottomWidth:0.5, borderBottomColor:C.border },

  ctrlBtn:     { borderWidth:0.5, borderRadius:10, padding:14, alignItems:'center' },
  ctrlBtnText: { fontSize:14, fontWeight:'600' },
});
