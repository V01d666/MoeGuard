import {useState, type SyntheticEvent} from 'react';
import type {Portrait} from './workshop.ts';
import {tablerPaths} from './tablerPaths.ts';

export function MoeGuardMark() {
  return <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M3 18.5a9.5 9.5 0 0 1 2-4.2L2.8 7.4c2.8.5 5.2 1.8 7.1 3.7a10 10 0 0 1 3.8-.4c.4-2.8 1.8-5.4 4-7.7 1.7 2.9 2.4 6.3 2 9.8a9.6 9.6 0 0 1 1.4 5.7 1 1 0 0 1-1.9.2A7.4 7.4 0 0 0 12 13a7.4 7.4 0 0 0-7.2 5.9 1 1 0 0 1-1.8-.4Z"/>
  </svg>;
}

export function Glyph({name, size = 18}: {name:keyof typeof tablerPaths|'dice'; size?:number}) {
  if (name === 'dice') return <svg className="glyph glyph-dice" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 2 21 7v10l-9 5-9-5V7Z M3 7l9 5 9-5M12 12v10"
      fill="none" stroke="currentColor" strokeWidth="1.55" strokeLinejoin="round"/>
    <g fill="currentColor">
      <circle cx="12" cy="6.9" r="1.1"/>
      <ellipse cx="6.1" cy="12.3" rx=".95" ry="1.17" transform="rotate(-24 6.1 12.3)"/>
      <ellipse cx="9" cy="16.3" rx=".95" ry="1.17" transform="rotate(-24 9 16.3)"/>
      <ellipse cx="17.9" cy="11.6" rx=".95" ry="1.17" transform="rotate(24 17.9 11.6)"/>
      <ellipse cx="16.6" cy="14.6" rx=".95" ry="1.17" transform="rotate(24 16.6 14.6)"/>
      <ellipse cx="15.3" cy="17.6" rx=".95" ry="1.17" transform="rotate(24 15.3 17.6)"/>
    </g>
  </svg>;
  return <svg className={`glyph glyph-${name}`} width={size} height={size} viewBox="0 0 24 24"
    fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {tablerPaths[name].map(path=><path key={path} d={path}/>)}
  </svg>;
}
const palette = ['#80add6','#e4a7b8','#a2a3d5','#78b6d5','#8ec7c0','#f2bb86','#adc0d8','#a7b499','#d9a4c7'];

function normalizedThumbnail(image:HTMLImageElement) {
  const canvas = document.createElement('canvas');
  const scale = Math.min(1,384/Math.max(image.naturalWidth,image.naturalHeight));
  const width = canvas.width = Math.max(1,Math.round(image.naturalWidth*scale));
  const height = canvas.height = Math.max(1,Math.round(image.naturalHeight*scale));
  const context = canvas.getContext('2d',{willReadFrequently:true});
  if (!context) return;
  context.drawImage(image,0,0,width,height);
  const pixels = context.getImageData(0,0,width,height).data;
  let left=width, top=height, right=-1, bottom=-1;
  for (let y=0;y<height;y++) {
    for (let x=0;x<width;x++) {
      if (pixels[(y*width+x)*4+3]<20) continue;
      left=Math.min(left,x); right=Math.max(right,x);
      top=Math.min(top,y); bottom=Math.max(bottom,y);
    }
  }
  if (right<left || bottom<top || (left===0&&top===0&&right===width-1&&bottom===height-1)) return;
  left=Math.max(0,left-1); top=Math.max(0,top-1);
  right=Math.min(width-1,right+1); bottom=Math.min(height-1,bottom+1);
  const cropWidth=right-left+1, cropHeight=bottom-top+1;
  const crop = context.getImageData(left,top,cropWidth,cropHeight);
  canvas.width=cropWidth;canvas.height=cropHeight;
  context.putImageData(crop,0,0);
  return canvas.toDataURL('image/png');
}

function CardImage({src}: {src:string}) {
  const [normalized,setNormalized] = useState<string>();
  function onLoad(event:SyntheticEvent<HTMLImageElement>) {
    if (normalized) return;
    const crop=normalizedThumbnail(event.currentTarget);
    if (crop) setNormalized(crop);
  }
  return <img className="pet-art" src={normalized??src} alt="" draggable={false} onLoad={onLoad}/>;
}

export function RoleCardArt({portrait,poster}: {portrait:Portrait;poster?:string}) {
  const src=poster??(portrait.src.startsWith('data:image/')?portrait.src:undefined);
  return <span className="tile-portrait" aria-hidden="true">
    {src?<CardImage key={src} src={src}/>:<Art portrait={portrait} alt=""/>}
  </span>;
}
export function Art({portrait, alt, motion = false}: {portrait:Portrait; alt:string; motion?:boolean}) {
  if (portrait.src.startsWith('data:image/')) return <img className={motion ? 'pet-art moving':'pet-art'} src={portrait.src} alt={alt}/>;
  const tone = palette[Number(portrait.src.slice(5))] ?? palette[3];
  return <svg className={motion ? 'pet-art moving':'pet-art'} viewBox="0 0 208 185" aria-hidden="true">
    <ellipse cx="104" cy="160" rx="59" ry="11" fill={tone} opacity=".18"/>
    <path d="M55 76Q50 43 77 46Q90 25 114 43Q154 34 159 67Q172 86 155 116Q142 148 104 153Q61 149 48 118Q38 93 55 76Z" fill={tone}/>
    <path d="M64 98Q64 78 85 76Q101 76 113 65Q125 79 144 85Q153 116 132 130Q100 149 73 127Q63 118 64 98Z" fill="#fff4f0"/>
    <path d="M76 94Q83 86 90 93m27 0q7-8 14 0" fill="none" stroke="#3c5773" strokeWidth="4" strokeLinecap="round"/>
    <path d="M99 110q6 5 12 0" fill="none" stroke="#9b718d" strokeWidth="2.6" strokeLinecap="round"/>
    <ellipse cx="76" cy="108" rx="6" ry="3" fill="#f1b5c4"/><ellipse cx="133" cy="108" rx="6" ry="3" fill="#f1b5c4"/>
    <path d="M101 134l14 6-11 14-13-13z" fill="#e7f4ff"/><path d="m143 55 10-12 4 15-15 4z" fill="#f3c2d0"/>
    <path d="m174 52 2 6 6 2-6 2-2 6-2-6-6-2 6-2zm-142 68 2 5 5 2-5 2-2 5-2-5-5-2 5-2z" fill={tone} opacity=".7"/>
  </svg>;
}
