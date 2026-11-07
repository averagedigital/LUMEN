import {createInterface} from 'node:readline';
import {chromium} from 'playwright';
import {captureSource, SourceRejected} from './source_capture.ts';

const browser=await chromium.launch({headless:true,args:['--disable-partial-raster']});
try {
  for await(const line of createInterface({input:process.stdin,crlfDelay:Infinity})) {
    const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'en-US',timezoneId:'UTC',
      colorScheme:'light',reducedMotion:'reduce',serviceWorkers:'block',acceptDownloads:false});
    try {
      if(line.length>100000) throw new SourceRejected('source request size limit');
      const result=await captureSource(await context.newPage(),JSON.parse(line));
      process.stdout.write(JSON.stringify({...result,status:'captured',reference:result.reference.toString('base64')})+'\n');
    } catch(error) {
      process.stdout.write(JSON.stringify({status:error instanceof SourceRejected?'rejected':'resource_error',error:error instanceof Error?error.message.slice(0,300):'capture failed'})+'\n');
    } finally {await context.close();}
  }
} finally {await browser.close();}
