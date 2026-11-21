import test from 'node:test';
import assert from 'node:assert/strict';
import {parseLyrics,activeLine,lyricTime,audioTime,fitAnchors,wordSegments,rankCandidates,cleanTitle,exportLrc} from '../dist/lyrics.mjs';

test('LRC repeated timestamps, file offset, ordering and Unicode',()=>{
  const parsed=parseLyrics('[ar:Test]\n[offset:500]\n[00:15.00][00:45.00]Šibenice\n[00:10.25]First line\n');
  assert.deepEqual(parsed.lines.map(l=>[l.time,l.text]),[[10.75,'First line'],[15.5,'Šibenice'],[45.5,'Šibenice']]);
  assert.equal(activeLine(parsed.lines,10),-1);assert.equal(activeLine(parsed.lines,15.5),1);
});
test('plain text stays untimed until the user supplies timing',()=>{
  const parsed=parseLyrics('First line\n\n日本語の歌詞\nLast line');
  assert.equal(parsed.timed,false);assert.equal(parsed.lines.length,3);
  assert.equal(activeLine(parsed.lines,100),-1);
});
test('a finished recognition line is not highlighted through a later gap',()=>{
  const rows=[{time:0,end:2,text:'First phrase'},{time:10,end:12,text:'Later phrase'}];
  assert.equal(activeLine(rows,1),0);assert.equal(activeLine(rows,3),-1);
  assert.equal(activeLine(rows,10.1),1);assert.equal(activeLine(rows,13),-1);
});
test('an old overlapping line does not reactivate after a newer line ends',()=>{
  assert.equal(activeLine([{time:0,end:20},{time:5,end:7}],8),-1);
});
test('word interpolation cannot extend beyond its own phrase end',()=>{
  const words=wordSegments({time:0,end:1,text:'One two',words:[]},10,20);
  assert.ok(words.at(-1).end<=1);
});
test('enhanced LRC preserves actual word times, including repeated lines',()=>{
  const parsed=parseLyrics('[00:10.00][00:30.00]<00:10.00>Hello <00:11.50>world');
  assert.equal(parsed.enhanced,true);
  assert.deepEqual(parsed.lines[0].words.map(w=>w.time),[10,11.5]);
  assert.deepEqual(parsed.lines[1].words.map(w=>w.time),[30,31.5]);
  assert.equal(wordSegments(parsed.lines[0],14,200)[0].end,11.5);
});
test('intro offset and drift map in both directions without altering audio',()=>{
  const fit=fitAnchors({lyric:10,audio:15},{lyric:110,audio:117});
  assert.equal(fit.scale,1.02);assert.ok(Math.abs(fit.offset-4.8)<1e-9);
  assert.equal(audioTime(110,fit.offset,fit.scale),117);
  assert.ok(Math.abs(lyricTime(117,fit.offset,fit.scale)-110)<1e-9);
  assert.equal(activeLine([{time:10,text:'a'}],lyricTime(14,5,1)),-1);
  assert.equal(activeLine([{time:10,text:'a'}],lyricTime(15,5,1)),0);
  assert.throws(()=>fitAnchors({lyric:10,audio:10},{lyric:10,audio:40}));
  assert.throws(()=>fitAnchors({lyric:10,audio:50},{lyric:50,audio:10}));
});
test('word estimates stay bounded by the line and are separate from real timestamps',()=>{
  const words=wordSegments({time:10,text:'A longer word',words:[]},16,100);
  assert.equal(words[0].time,10);
  assert.ok(Math.abs(words.at(-1).end-11.5)<1e-9); // Don't stretch three words through an instrumental gap.
  assert.ok(words.at(-1).end<=16);
  assert.ok(words[1].end-words[1].time>words[0].end-words[0].time);
});
test('model word end-times leave instrumental gaps unfilled',()=>{
  const words=wordSegments({time:0,text:'One two',words:[{text:'One ',time:0,end:.5},{text:'two',time:3,end:3.5}]},5,10);
  assert.equal(words[0].end,.5);assert.equal(words[1].end,3.5);
});
test('partial word anchors retain their audio times without exporting estimates',()=>{
  const line = {
    time: 10, end: 14, text: 'The night begins to glow', words: [],
    wordAnchors: [{index: 1, time: 10.5, end: 11}, {index: 3, time: 12, end: 12.25}],
  };
  const words = wordSegments(line, 15, 60);
  assert.equal(words.map(word => word.text).join(''), line.text);
  assert.deepEqual(words.slice(0, 4).map(word => [word.time, word.end]), [
    [10, 10.5], [10.5, 11], [11, 12], [12, 12.25],
  ]);
  assert.ok(words.at(-1).end <= line.end);
  assert.match(exportLrc([line], 0, 1), /\[00:10\.00\]The night begins to glow/);
});
test('auto-selection requires artist, title, duration and synchronized lyrics',()=>{
  const rows=[{id:1,trackName:'Joyride',artistName:'Roxette',duration:270,syncedLyrics:'[00:00]a'},
              {id:2,trackName:'Joyride',artistName:'Other artist',duration:270,syncedLyrics:'[00:00]a'},
              {id:3,trackName:'Joyride',artistName:'Roxette',duration:235,syncedLyrics:'[00:00]a'}];
  const ranked=rankCandidates(rows,{title:'Joyride',artist:'Roxette',duration:270.03});
  assert.equal(ranked[0].id,1);assert.equal(ranked.filter(r=>r.safe).length,1);
  assert.equal(rankCandidates(rows,{title:'Joyride',artist:'',duration:270})[0].safe,false);
});
test('lookup requires the displayed second and ranks by millisecond difference',()=>{
  const track = {title:'Example', artist:'Artist', duration:231.04};
  const candidate = (id, duration) => ({id, trackName:track.title, artistName:track.artist, duration, syncedLyrics:'[00:00]line'});
  const matches = rankCandidates([candidate(1,231), candidate(2,232), candidate(3,231.07), candidate(4,231.12), candidate(5,230.999)], track);
  assert.deepEqual(matches.map(row => row.id), [3, 1]);
});

test('title cleanup keeps musical version distinctions',()=>{
  assert.equal(cleanTitle('If It Makes You Happy Closed-Captioned'),'If It Makes You Happy');
  assert.equal(cleanTitle('Song (Official Music Video)'),'Song');
  assert.equal(cleanTitle('Song (Live) [Remix]'),'Song (Live) [Remix]');
});
test('export bakes in the adjusted timing and preserves actual word timing',()=>{
  const lines=parseLyrics('[00:10.00]<00:10.00>Hello <00:11.00>world').lines;
  const output=exportLrc(lines,5,1.1,'Example','Artist');
  assert.match(output,/\[00:16.00\]<00:16.00>Hello <00:17.10>world/);
  assert.equal(parseLyrics(output).lines[0].time,16);
});
