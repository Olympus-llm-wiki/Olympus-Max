// Artifact Tool authoring surface; paths and allowed edits are supplied by the local runner.
import fs from 'node:fs/promises';
import {FileBlob, SpreadsheetFile} from '@oai/artifact-tool';
const cfg=JSON.parse(await fs.readFile(process.argv[2],'utf8'));
const previewOnly=process.argv[3]==='preview';
const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(previewOnly?cfg.output:cfg.template));
const sheet=wb.worksheets.getItem(cfg.sheet);
if(!previewOnly){
for(const [address,value] of Object.entries({...cfg.values,...cfg.headers})) {
  sheet.getRange(address).values=[[typeof value==='string'&&value.startsWith('=')?"'"+value:value]];
}
for(const [address,value] of Object.entries(cfg.footers)){
  const row=Number(address.match(/\d+/)[0]);const range=sheet.getRange(`A${row}:AJ${row}`);
  range.merge();range.values=[[value]];range.format.font={name:'Arial',size:9,color:'#172634'};
  range.format.horizontalAlignment='left';range.format.verticalAlignment='center';range.format.wrapText=true;range.format.rowHeight=24;
}
for(const [cell,fill] of Object.entries(cfg.fills))sheet.getRange(cell).format.fill=fill;
for(const [cell,format] of Object.entries(cfg.number_formats))sheet.getRange(cell).setNumberFormat(format);
for(const [address,text] of Object.entries(cfg.notes))wb.notes.add({id:`${sheet.name}:${address}`,target:{cell:{sheetName:sheet.name,sheetId:sheet.sheetId,address}},authorId:'',createdAt:'',body:{plainText:text}});
wb.recalculate();
await(await SpreadsheetFile.exportXlsx(wb)).save(cfg.authored);
console.log(JSON.stringify({authored:true,values:Object.keys(cfg.values).length,notes:Object.keys(cfg.notes).length}));
}
if(previewOnly&&cfg.preview){
  for(const [name,range] of Object.entries(cfg.preview)){
    const image=await wb.render({sheetName:cfg.sheet,range,scale:1.25,format:'png'});
    await fs.writeFile(name,new Uint8Array(await image.arrayBuffer()));
  }
}
