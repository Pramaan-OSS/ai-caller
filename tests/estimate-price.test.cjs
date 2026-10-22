const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');const path=require('node:path');
const context=vm.createContext({});vm.runInContext(fs.readFileSync(path.join(__dirname,'../ai_caller/static/estimate-price.js'),'utf8'),context);
for(const typical of [undefined,0,50])test(`fake estimate display typical ${typical}`,async()=>{
 const fake={estimate:async()=>({sentence:'Review this action.',priceMaxMicro:100,priceTypicalMicro:typical})};const estimate=await fake.estimate();const html=context.estimatePriceHtml(estimate,String);
 if(typical===undefined)assert.equal(html,'<p class="meta">Review this action.</p>');else {assert.ok(html.includes(`<strong>usually about $${(typical/1_000_000).toFixed(6)}</strong>`));assert.ok(html.includes('<small>at most $0.000100</small>'));}
});
